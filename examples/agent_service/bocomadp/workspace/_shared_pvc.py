# -*- coding: utf-8 -*-
"""共享 PVC 模式：agent 级共享 PVC + 池 Pod hash 路由，无占用标记。

架构
----

::

    PVC: as-ws-{agent_hash} (ReadWriteMany, hash(user::agent))
         │
         ├── sessions/{sess_A}/          ← 分目录隔离，与 Pod 无关
         └── sessions/{sess_B}/

    PVC: as-ws-shared-{agent_shared_hash} (ReadWriteMany, hash(agent_id))
         │
         ├── shared/skills/              ← 所有 (user, agent) 会话共享
         └── shared/.mcp                 ← 所有 (user, agent) 会话共享

池（hash 路由，按 source 分流）
-------------------------------

Manager 按 ``max_active_pods`` 预创建 N 个 Pod
（``as-ws-{pool_hash}-0 .. N-1``），会话经 ``hash(session_id) % N``
固定路由到某个 Pod，**不做任何占用标记**——同一 Pod 可被多个
会话并发复用（per_agent 共享语义的 N Pod 变体）。Pod 缺失/不可用
时顺延到下一个；全池空闲超 ``pool_idle_ttl`` 由 sweeper 全部回收
（PVC 保留，下次访问懒重建）。

会话按来源分两套池（``pool_hash`` 不同，标签值随之分离，sweeper
按标签分组回收无需感知 source）：

- **adp 池**：``hash(user::agent)``，共享 PVC 挂载为读写；
- **非 adp 池**（``NON_ADP_SESSION_SOURCE``，如 deerflow）：
  ``hash(user::agent::deerflow)``，池大小固定 1，共享 PVC 挂载为
  只读——skills/.mcp 只读，session 工作目录仍在可写的 user PVC。

共享 PVC 初始化（丢弃重装）：首个 adp 会话（rw 挂载）补齐
``shared/`` 目录与 ``.mcp``；非 adp 会话先到时的写失败降级容忍
（MCP 暂不可用，adp 会话补齐后自愈）。

零框架改动 — 所有逻辑通过子类覆盖实现，不动 ``agentscope`` 一行代码。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import time
from typing import Any

from agentscope._logging import logger
from agentscope.app.workspace_manager import (
    K8sWorkspaceManager,
    IsolationPolicy,
)
from agentscope.workspace import K8sWorkspace
from agentscope.workspace._gateway_client import GatewayClient
from agentscope.workspace._k8s._k8s_backend import K8sBackend
from agentscope.workspace._k8s._constants import (
    GATEWAY_HOME,
    POD_WORKDIR,
    _k8s_safe_name,
)

# ── reuse parent's utility constants ───────────────────────────────
from agentscope.workspace._utils import (
    DEFAULT_GATEWAY_LOG,
    DEFAULT_GATEWAY_SCRIPT,
    DEFAULT_GATEWAY_VENV,
    DEFAULT_MCP_FILE,
    DEFAULT_SKILLS_DIR,
)

# ── user-data layout constants ─────────────────────────────────────
#: Workspace-relative root of the user working area.
DEFAULT_USER_DATA_DIR = "user-data"

#: Workspace-relative working directory for temporary files.
DEFAULT_USER_SCRATCH_DIR = "scratch"

#: Workspace-relative directory for final deliverables.
DEFAULT_USER_OUTPUTS_DIR = "outputs"

#: Workspace-relative directory for user-uploaded files (sandbox-safe,
#: inside workdir so that dual-PVC session PVC and shared-PVC
#: ``/workspace/sessions/{id}`` both isolate uploads per session).
DEFAULT_USER_UPLOADS_DIR = "uploads"


_K8S_LABEL_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9-_.]")


#: 默认 K8s API 请求超时（秒）：kubernetes_asyncio 默认无限等待，
#: apiserver 假死/网络抖动时协程会永久挂起，此处统一注入超时。
_K8S_REQUEST_TIMEOUT_SECS = 30.0


def _patch_api_client_timeout(api_client: Any, timeout: float) -> None:
    """给 ApiClient 注入默认请求超时（不影响显式传入的调用）。

    kubernetes_asyncio 的 API 方法把 ``_request_timeout`` 透传给
    :meth:`call_api`，默认 ``None`` 表示无限等待。包装 ``call_api``
    在未显式指定时填入默认值，避免 apiserver 假死导致协程永久挂起。
    """
    orig_call_api = api_client.call_api

    def call_api(*args: Any, **kwargs: Any) -> Any:
        if not kwargs.get("_request_timeout"):
            kwargs["_request_timeout"] = timeout
        return orig_call_api(*args, **kwargs)

    api_client.call_api = call_api

#: Pod annotation key：池 Pod 最近活跃时间（unix 秒，字符串）。
#: hash 路由每次选中 Pod 时由 ``_touch_last_active`` 刷新；sweeper
#: 据此判断 agent 的池是否整体闲置（超 ``pool_idle_ttl`` 全回收）。
#: annotation 存 K8s 侧，不依赖进程内存，多实例部署下全局一致。
LAST_ACTIVE_ANNOTATION = "agentscope.pool.last-active-at"

#: 非 adp 来源会话的 workspace_id 前缀（deerflow 创建会话时用
#: ``deerflow-{uuid}`` 作为 workspace_id）：get_workspace 据此判定
#: 会话来源——此类会话挂载共享 PVC 为只读、使用独立池（大小固定
#: 1）。框架 SessionSource 枚举无法扩展成员（pydantic 校验拒绝
#: 任意字符串，读回时 model_validate 同样拒绝），故用自由字符串
#: 的 workspace_id 承载来源标记，零框架改动。
NON_ADP_WORKSPACE_PREFIX = "deerflow-"


def _k8s_safe_label(value: str, max_len: int = 63) -> str:
    """清洗 K8s label 值：仅保留字母数字与 ``- _ .``，首尾必须字母数字。

    原始 agent_id / session_id / workspace_id 可能含空格、Unicode
    连字符（如 U+2011）等非法字符，直接写入 label 会被 API server
    以 422 拒绝。写入与按 label 查询两侧必须使用本函数保证一致。
    """
    cleaned = _K8S_LABEL_UNSAFE_RE.sub("-", value)
    cleaned = cleaned.strip("-._")
    return (cleaned or "x")[:max_len].rstrip("-._")


class SharedPvcK8sWorkspace(K8sWorkspace):
    """K8sWorkspace 子类：session 挂载到池 Pod，共享 agent 级 PVC。

    覆盖父类方法实现：

    - Pod 名 = 池 Pod 名（hash 路由）或 session 级（按需）
    - PVC 名 = agent 级（``as-ws-{agent_hash}``），所有 session 共享
    - workdir = ``/workspace/sessions/{session_id}``（路径隔离）
    - skills/.mcp → ``/workspace/shared/``（共享）
    - 关闭时按需 Pod 删除、池 Pod 不动（由 Manager sweeper 管理）
    """

    def __init__(
        self,
        *,
        # ── 新增: 共享 PVC 参数 ──
        shared_pvc_name: str = "",
        session_id: str = "",
        shared_pvc_access_mode: str = "ReadWriteMany",
        pod_name: str = "",
        # ── agent 级共享 PVC（skills/.mcp）：空串表示不挂载 ──
        agent_shared_pvc_name: str = "",
        # ── 共享 PVC 是否只读挂载（非 adp 会话为 True） ──
        shared_read_only: bool = False,
        refresh_heartbeat_interval: float = 0.0,
        # ── 透传给父类的所有参数 ──
        workspace_id: str | None = None,
        kubeconfig: str | None = None,
        namespace: str = "agentscope",
        image: str = "python:3.11-slim",
        image_pull_policy: str = "IfNotPresent",
        image_pull_secrets: list[str] | None = None,
        resources: dict[str, Any] | None = None,
        node_selector: dict[str, str] | None = None,
        tolerations: list[dict[str, Any]] | None = None,
        service_account: str | None = None,
        gateway_port: int = 5600,
        extra_pip: list[str] | None = None,
        storage_class: str | None = None,
        storage_size: str = "1Gi",
        delete_pvc_on_close: bool = False,
        env: dict[str, str] | None = None,
        instructions: str = "",
        default_mcps: list[Any] | None = None,
        skill_paths: list[str] | None = None,
    ) -> None:
        # 先让父类初始化（设置 workdir = POD_WORKDIR 等）
        super().__init__(
            workspace_id=workspace_id,
            kubeconfig=kubeconfig,
            namespace=namespace,
            image=image,
            image_pull_policy=image_pull_policy,
            image_pull_secrets=image_pull_secrets,
            resources=resources,
            node_selector=node_selector,
            tolerations=tolerations,
            service_account=service_account,
            gateway_port=gateway_port,
            extra_pip=extra_pip,
            storage_class=storage_class,
            storage_size=storage_size,
            delete_pvc_on_close=delete_pvc_on_close,
            env=env,
            instructions=instructions,
            default_mcps=default_mcps,
            skill_paths=skill_paths,
        )

        # ── 覆盖共享 PVC 状态 ──
        self._shared_pvc_name: str = shared_pvc_name
        self._session_id: str = session_id
        self._shared_pvc_access_mode: str = shared_pvc_access_mode
        self._assigned_pod_name: str = pod_name
        self._agent_shared_pvc_name: str = agent_shared_pvc_name
        self._shared_read_only: bool = shared_read_only

        # ── 心跳间隔（pool_idle_ttl 派生，由 Manager 注入）：
        # 供 SlotReleaseMiddleware 在 run 期间周期性刷新池 Pod
        # 活跃信号，多实例部署下长 run 不会被其他实例的 sweeper
        # 误判“全池闲置”回收。按需模式（无池 Pod）无此需求 →
        # 置 0，middleware 探测后跳过心跳。
        self.refresh_heartbeat_interval: float = (
            refresh_heartbeat_interval if pod_name else 0.0
        )

        # ── run 使用标记：sweeper 对 busy 的 ws 续期而非 close，
        # 防止超 TTL 的长 run 被拆 backend（详见 set_run_active）。
        self._run_active: bool = False

        # ── 覆盖工作目录为 session 子目录 ──
        self.workdir = f"{POD_WORKDIR}/sessions/{self._session_id}"

        # ── 更新 instructions：描述真实的共享 PVC 布局 ──
        # 不能直接用 DEFAULT_WORKSPACE_INSTRUCTIONS，因为它的
        # 模板把 skills/ 放在 workdir 下，而共享 PVC 下 skills
        # 在 /workspace/shared/skills/，与 workdir 不在同一路径。
        self.instructions = (
            "<workspace>你可以访问一个位于{workdir}的{backend}工作区，"
            "其目录结构如下："
            "\n\n```"
            "\n/workspace/"
            "\n├── shared/            # 所有会话共享"
            "\n│   ├── skills/        # 可复用的技能目录"
            "\n│   └── .mcp           # MCP 配置"
            "\n└── sessions/"
            "\n    └── <session>/    # 你的工作目录"
            "\n        ├── data/      # 卸载的多模态文件——系统管理"
            "\n        ├── sessions/  # 卸载的会话上下文与工具结果——系统管理"
            "\n        └── user-data/ # 用户工作目录"
            "\n            ├── scratch/   # 临时文件工作目录"
            "\n            ├── uploads/    # 用户上传的文件——文档类自动转 .md，文本类保留原样"
            "\n            └── outputs/    # 最终交付物——完成的文件写到这里，不要询问"
            "\n```"
            "\n\n你的工作目录是{workdir}。"
            "这个工作区是你的个人工作环境，你有责任保持其整洁、结构清晰、"
            "并便于长期维护。"
            "\n\n### 文件管理"
            "\n- 临时和中间文件（草稿、实验、构建产物、调试脚本）放在"
            "`user-data/scratch/`。"
            "\n- 你生成的任务最终成果——报告、文档、代码、导出文件、图片——"
            "**必须**直接写入`user-data/outputs/`。不要询问用户保存位置，"
            "自动应用此规则。"
            "\n- 如果交付物最初在`scratch/`中开始（例如迭代过程中），"
            "完成后请将成品文件复制到`outputs/`。"
            "\n- 在脚本和命令中，优先使用相对路径（如`hello.txt`、"
            "`../outputs/report.md`），避免硬编码绝对路径。"
            "\n\n### 上传文件"
            "\n- 用户上传的文档类文件（.xlsx/.docx/.pdf 等）会自动转换为 Markdown，"
            "转换结果是与原文件同名的 .md，存放在`user-data/uploads/`目录下"
            "（例如 report.xlsx → report.md）。"
            "\n- 文本/代码类文件（.txt/.md/.csv/.json/.log/.py 等）不转换为 .md，"
            "保留原始文件——直接用文件读取工具（Read/bash cat）读取"
            "`user-data/uploads/`下的原文件。"
            "\n- 分析文档类文件时，必须优先读取同名 .md 版本——不要自行解析原始二进制文件"
            "（如 .xlsx/.docx/.pdf 等），也不要为了读取文件而 pip install 安装包。"
            "\n- 如果同名 .md 不存在，可以使用当前环境中已有的包自行解析原始二进制文件"
            "——但禁止安装新包。"
            "\n- 用户上传的图片（.png/.jpg/.jpeg/.webp）不会转换为 .md，上传时已"
            "固化为 base64 存于上传元数据。解析图片**必须**调用 "
            "view_image_tool(virtual_path=..., question=...)：先调用 "
            "list_uploaded_files 获取图片的 virtual_path 再传给该工具。"
            "\n- 禁止用 Read/bash/Python 直接读取二进制图片文件（会得到乱码），"
            "也不要尝试用编程方式解析图片（环境可能没有图像处理库）。"
            "\n\n### 项目目录"
            "\n- 为每个任务或项目在工作目录下创建专属子目录。"
            "\n- 项目子目录命名要简洁、有描述性，并以创建日期的绝对时间前缀，"
            "例如`20240315_web-scraper`，以便长期可辨识。"
            "\n- 始终在项目根目录创建`README.md`，记录："
            "\n  - 项目内容概述"
            "\n  - 创建日期的绝对时间"
            "\n  - 有助于以后恢复工作的关键决策或上下文"
            "\n\n### 技能"
            "\n- 技能是位于/workspace/shared/skills/的共享资源。"
            "\n- 每个技能都有一个SKILL.md，包含完整说明——使用Read工具查看。"
            "\n\n### Python 环境"
            "\n- 沙箱预置了Python虚拟环境`/root/.agentscope/.venv`。"
            "\n- 执行Python脚本前，先激活该环境：`source /root/.agentscope/.venv/bin/activate`"
            "\n</workspace>"
        ).format(
            backend="Kubernetes-based (shared-PVC)",
            workdir=self.workdir,
        )

    # ── user-data paths (relative to workdir) ────────────────────

    @property
    def _user_data_dir(self) -> str:
        """``${workdir}/user-data`` — user working area root."""
        return self.get_backend().join_path(self.workdir, DEFAULT_USER_DATA_DIR)

    @property
    def _user_workspace_dir(self) -> str:
        """``${workdir}/user-data/scratch`` — temp file working directory."""
        return self.get_backend().join_path(
            self._user_data_dir,
            DEFAULT_USER_SCRATCH_DIR,
        )

    @property
    def _user_outputs_dir(self) -> str:
        """``${workdir}/user-data/outputs`` — final deliverables."""
        return self.get_backend().join_path(
            self._user_data_dir,
            DEFAULT_USER_OUTPUTS_DIR,
        )

    @property
    def _user_uploads_dir(self) -> str:
        """``${workdir}/user-data/uploads`` — user-uploaded files."""
        return self.get_backend().join_path(
            self._user_data_dir,
            DEFAULT_USER_UPLOADS_DIR,
        )

    async def _ensure_workspace_layout(self) -> None:
        """Create the standard workspace directories plus user-data/.

        The parent layout covers ``data/``, ``skills/``, ``sessions/``
        and the gateway home; on top of that this session workspace
        guarantees ``user-data/scratch/``, ``user-data/outputs/`` and
        ``user-data/uploads/`` exist so the model always has a writable
        working area, a destination for final deliverables and a home
        for user-uploaded files (used by the upload endpoint).
        """
        try:
            await super()._ensure_workspace_layout()
        except Exception:
            # 只读挂载 + 共享 PVC 尚未初始化（无 skills/.mcp）时，
            # 父类 seed .mcp 会写失败；不阻断初始化——目录与配置
            # 由后续 adp 会话（rw 挂载）补齐，属预期降级。
            if not self._shared_read_only:
                raise
            logger.warning(
                "%s: shared PVC not initialized yet; skip layout "
                "seed on read-only mount (workspace=%r)",
                type(self).__name__,
                self.workspace_id,
            )
        backend = self.get_backend()
        await backend.exec_shell(
            [
                "mkdir",
                "-p",
                self._user_data_dir,
                self._user_workspace_dir,
                self._user_outputs_dir,
                self._user_uploads_dir,
            ],
            cwd="/",
        )

    async def _setup_skills(self) -> None:
        """技能安装：只读挂载下 ``list_dir``/写入失败均容忍。

        共享 PVC 未初始化时 ``skills/`` 目录不存在，父类
        ``list_dir`` 会抛异常；rw 挂载（adp 会话）补建后自愈。
        """
        if not self._shared_read_only:
            await super()._setup_skills()
            return
        try:
            await super()._setup_skills()
        except Exception as e:
            logger.warning(
                "%s: skill setup skipped on read-only mount: %s",
                type(self).__name__,
                e,
            )

    # ── run 使用标记（供 sweeper 续期，防长 run 被回收） ─────

    def set_run_active(self, active: bool) -> None:
        """标记 run 使用中：sweeper 对 busy 条目续期，避免长 run 被 close。

        每次 run 开始时置 True（SlotReleaseMiddleware），结束时置
        False。``SharedPvcK8sWorkspaceManager._sweep_once`` 对
        ``_run_active`` 为 True 的 ws 刷新访问时间戳（跳过 TTL
        回收）：否则超过 TTL 的长 run 会被 close 拆 backend，
        后续工具执行报错。
        """
        self._run_active = active

    async def refresh_active(self) -> None:
        """刷新池 Pod 活跃信号（last-active annotation）。

        供 SlotReleaseMiddleware 在 run 开始/结束时调用，超长 run
        期间由心跳任务周期性调用（间隔见
        ``refresh_heartbeat_interval``）：空闲回收（sweeper
        ``_sweep_pool``）据此判断 agent 的池是否闲置。
        annotation 在 K8s 侧全局一致，多实例部署下也能正确保护
        其他实例上进行中的长 run 不被误回收。按需模式（无池
        Pod）或未初始化实例直接跳过；失败静默（仅影响空闲判定）。
        """
        if not self._assigned_pod_name or self._v1 is None:
            return
        from kubernetes_asyncio.client.rest import ApiException

        try:
            await self._v1.patch_namespaced_pod(
                self._pod_name,
                self._namespace,
                {
                    "metadata": {
                        "annotations": {
                            LAST_ACTIVE_ANNOTATION: str(int(time.time())),
                        },
                    },
                },
            )
        except ApiException:
            pass

    async def _gateway_healthy(self) -> bool:
        """探测 Pod 内网关 ``/health``，健康返回 True。

        预热复用以它为准：健康 → 跳过启动，
        直接绑定 :class:`GatewayClient`。
        """
        try:
            backend = self.get_backend()
        except RuntimeError:
            return False
        probe = GatewayClient(
            backend=backend,
            gateway_port=self.gateway_port,
            timeout=30.0,
            gateway_log_path=self._gateway_log,
        )
        try:
            return await probe.health()
        except Exception:
            return False

    async def _setup_mcp_gateway(self) -> None:
        """复用 Pod 上健康运行的网关（温池预热），否则回退框架逻辑。

        框架默认每次挂载都 pkill + 重启网关并轮询 /health（1~3s）；
        预热保证空闲 Pod 的网关常驻健康，此处探测命中即可直接绑定，
        跳过进程启动开销。探测失败（网关死/Pod 重建）时回退
        ``super()`` 完整启动，保证正确性。
        """
        if await self._gateway_healthy():
            logger.info(
                "%s: reusing healthy gateway on pod %r",
                type(self).__name__,
                self._pod_name,
            )
            self._gateway = GatewayClient(
                backend=self.get_backend(),
                gateway_port=self.gateway_port,
                timeout=30.0,
                gateway_log_path=self._gateway_log,
            )
            self._mcps = list(await self._gateway.list_mcps())
            return
        try:
            await super()._setup_mcp_gateway()
        except Exception:
            if not self._shared_read_only:
                raise
            # 只读挂载 + 共享 PVC 未初始化：.mcp 缺失时网关起不来，
            # MCP 功能暂时降级（工具执行不受影响），adp 会话补齐
            # 共享区后自愈。
            self._gateway = None
            logger.warning(
                "%s: MCP gateway unavailable on read-only mount "
                "(shared PVC not initialized); degraded until an "
                "adp session seeds it (workspace=%r)",
                type(self).__name__,
                self.workspace_id,
            )

    # ── 覆盖 provisioning: 支持池 Pod 名 ─────────────────────

    async def _provision_backend(self) -> None:
        """创建或附加到 K8s Pod，支持温池预创建的 Pod。

        与父类的唯一区别：当 ``_assigned_pod_name`` 非空时，
        使用池 Pod 名而非从 workspace_id 派生。
        """
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config

        if self._kubeconfig:
            await k8s_config.load_kube_config(
                config_file=self._kubeconfig,
            )
        else:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                await k8s_config.load_kube_config()

        self._api_client = k8s_client.ApiClient()
        _patch_api_client_timeout(
            self._api_client,
            _K8S_REQUEST_TIMEOUT_SECS,
        )
        self._v1 = k8s_client.CoreV1Api(self._api_client)

        if self._assigned_pod_name:
            self._pod_name = self._assigned_pod_name
        else:
            self._pod_name = _k8s_safe_name(self.workspace_id)

        await self._ensure_namespace()
        await self._ensure_pvc()
        if self._agent_shared_pvc_name:
            await self._ensure_agent_shared_pvc()
        await self._ensure_pod()
        await self._wait_pod_running()

        self._backend = K8sBackend(
            api_client=self._api_client,
            namespace=self._namespace,
            pod_name=self._pod_name,
            container_name="workspace",
            workdir=POD_WORKDIR,
        )

    # ── 覆盖 property: skills/.mcp 指向共享区 ──────────────────

    @property
    def _skills_dir(self) -> str:
        """``/workspace/shared/skills`` — 所有 session 共享。"""
        return self.get_backend().join_path(
            POD_WORKDIR, "shared", DEFAULT_SKILLS_DIR,
        )

    @property
    def _mcp_file(self) -> str:
        """``/workspace/shared/.mcp`` — 所有 session 共享。"""
        return self.get_backend().join_path(
            POD_WORKDIR, "shared", DEFAULT_MCP_FILE,
        )

    # ── 覆盖 4 个 K8s 资源管理方法 ────────────────────────────

    async def _ensure_pvc(self) -> None:
        """使用 agent 级 PVC 名而非 Pod 名。

        其余逻辑与父类相同：exists → 检查 deletion_timestamp；
        not found → 创建。
        """
        from kubernetes_asyncio.client.rest import ApiException

        pvc_name = self._shared_pvc_name  # ← 唯一改动点
        try:
            pvc = await self._v1.read_namespaced_persistent_volume_claim(
                pvc_name,
                self._namespace,
            )
            if pvc.metadata and pvc.metadata.deletion_timestamp is not None:
                logger.info(
                    "SharedPvcK8sWorkspace: PVC %r is being deleted, "
                    "waiting...",
                    pvc_name,
                )
                await self._wait_pvc_deleted(pvc_name)
                await self._create_pvc(pvc_name)
        except ApiException as e:
            if e.status == 404:
                await self._create_pvc(pvc_name)
            else:
                raise

    async def _ensure_agent_shared_pvc(self) -> None:
        """确保 agent 级共享 PVC（skills/.mcp）存在。

        与 :meth:`_ensure_pvc`（user 级 PVC）逻辑一致，仅 PVC 名
        不同。共享 PVC 被该 agent 的所有 (user, agent) 会话共用，
        存续期间永不删除（skills 为 agent 级长期资产）。
        """
        from kubernetes_asyncio.client.rest import ApiException

        pvc_name = self._agent_shared_pvc_name
        try:
            pvc = await self._v1.read_namespaced_persistent_volume_claim(
                pvc_name,
                self._namespace,
            )
            if pvc.metadata and pvc.metadata.deletion_timestamp is not None:
                logger.info(
                    "SharedPvcK8sWorkspace: shared PVC %r is being "
                    "deleted, waiting...",
                    pvc_name,
                )
                await self._wait_pvc_deleted(pvc_name)
                await self._create_pvc(pvc_name)
        except ApiException as e:
            if e.status == 404:
                await self._create_pvc(pvc_name)
            else:
                raise

    async def _create_pvc(self, pvc_name: str) -> None:
        """使用可配置的 access mode（默认 ReadWriteMany）。

        其余逻辑与父类相同。
        """
        from kubernetes_asyncio import client as k8s_client

        access_modes = [self._shared_pvc_access_mode]  # ← 唯一改动点

        spec_kwargs: dict[str, Any] = {
            "access_modes": access_modes,
            "resources": k8s_client.V1VolumeResourceRequirements(
                requests={"storage": self._storage_size},
            ),
        }
        if self._storage_class is not None:
            spec_kwargs["storage_class_name"] = self._storage_class

        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name=pvc_name,
                namespace=self._namespace,
                labels={
                    "app.kubernetes.io/managed-by": "agentscope",
                    "agentscope.workspace.id": _k8s_safe_label(self.workspace_id),
                },
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(**spec_kwargs),
        )
        await self._v1.create_namespaced_persistent_volume_claim(
            self._namespace,
            pvc,
        )

    async def _create_pod(self) -> None:
        """Pod 挂载 agent 级共享 PVC，working_dir 指向 session 子目录。

        与父类有两处不同：
        1. volume claim_name → ``self._shared_pvc_name``
        2. working_dir → ``self.workdir``（session 子目录）
        """
        from kubernetes_asyncio import client as k8s_client

        container_env = None
        if self.env:
            container_env = [
                k8s_client.V1EnvVar(name=k, value=v)
                for k, v in self.env.items()
            ]

        container = k8s_client.V1Container(
            name="workspace",
            image=self._image,
            image_pull_policy=self._image_pull_policy,
            command=["sleep", "infinity"],
            working_dir=self.workdir,  # ← session 子目录
            ports=[
                k8s_client.V1ContainerPort(
                    container_port=self.gateway_port,
                ),
            ],
            resources=(
                k8s_client.V1ResourceRequirements(**self._resources)
                if self._resources
                else None
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="workspace-data",
                    mount_path=POD_WORKDIR,
                ),
                *(
                    [
                        k8s_client.V1VolumeMount(
                            name="workspace-shared",
                            mount_path=f"{POD_WORKDIR}/shared",
                            read_only=self._shared_read_only,
                        ),
                    ]
                    if self._agent_shared_pvc_name
                    else []
                ),
            ],
            env=container_env,
        )

        claim_name = self._shared_pvc_name  # ← agent 级 PVC

        volumes = [
            k8s_client.V1Volume(
                name="workspace-data",
                persistent_volume_claim=(
                    k8s_client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=claim_name,
                    )
                ),
            ),
            *(
                [
                    k8s_client.V1Volume(
                        name="workspace-shared",
                        persistent_volume_claim=(
                            k8s_client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=self._agent_shared_pvc_name,
                            )
                        ),
                    ),
                ]
                if self._agent_shared_pvc_name
                else []
            ),
        ]

        spec_kwargs: dict[str, Any] = {
            "restart_policy": "OnFailure",
            # 沙箱无优雅停机需求（数据在 PVC 已落盘、网关无状态），
            # 快杀让 Pod 删除可靠快速，避免 Terminating 卡满默认
            # 30s grace period 导致删除等待超时。
            "termination_grace_period_seconds": 5,
            "containers": [container],
            "volumes": volumes,
        }
        if self._node_selector:
            spec_kwargs["node_selector"] = self._node_selector
        if self._tolerations:
            spec_kwargs["tolerations"] = [
                k8s_client.V1Toleration(**t) for t in self._tolerations
            ]
        if self._service_account:
            spec_kwargs["service_account_name"] = self._service_account
        if self._image_pull_secrets:
            spec_kwargs["image_pull_secrets"] = [
                k8s_client.V1LocalObjectReference(name=s)
                for s in self._image_pull_secrets
            ]

        pod = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=self._pod_name,
                namespace=self._namespace,
                labels={
                    "app.kubernetes.io/managed-by": "agentscope",
                    "agentscope.workspace": "true",
                    "agentscope.workspace.id": _k8s_safe_label(self.workspace_id),
                },
            ),
            spec=k8s_client.V1PodSpec(**spec_kwargs),
        )
        await self._v1.create_namespaced_pod(self._namespace, pod)

    async def _teardown_backend(self) -> None:
        """按需模式：删除 session Pod；池模式：Pod 不动。

        hash 路由下池 Pod 无归属概念（可被多会话并发复用），
        生命周期由 Manager 的 sweeper 统一管理（空闲回收/死 Pod
        重建），session close 只拆本实例连接。共享 PVC 由 agent
        级别管理，不在 session 结束时清理。
        """
        from kubernetes_asyncio.client.rest import ApiException

        if self._v1 is not None and self._pod_name:
            if not self._assigned_pod_name:
                # ── 按需：删除 session Pod ──
                try:
                    await self._v1.delete_namespaced_pod(
                        self._pod_name,
                        self._namespace,
                    )
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(
                            "SharedPvcK8sWorkspace: Pod delete "
                            "failed: %s",
                            e,
                        )

            # 共享模式下绝不删除 PVC
            # （即使 _delete_pvc_on_close=True 也忽略，
            #  因为其他 session 可能正在使用）

        if self._api_client is not None:
            try:
                await self._api_client.close()
            except Exception:
                pass
            self._api_client = None
            self._v1 = None


# ── Manager ────────────────────────────────────────────────────────


class SharedPvcK8sWorkspaceManager(K8sWorkspaceManager):
    """管理 :class:`SharedPvcK8sWorkspace` 实例 + 池 Pod。

    与父类 :class:`K8sWorkspaceManager` 的区别：

    - 隔离策略固定为 ``PER_SESSION``（每个 session 独立 workspace 对象）
    - PVC 名称由 ``user_id::agent_id`` hash 派生（agent 级共享）
    - 缓存 key 仍是 session-scoped workspace_id
    - 池 Pod：按 ``hash(session_id) % N`` 固定路由，无占用标记，
      同一 Pod 可被多会话并发复用；不可用顺延下一个
    - 池生命周期：sweeper 删除不可用 Pod（下次访问补建）、
      全池闲置超 ``pool_idle_ttl`` 时整体回收（PVC 保留）
    """

    # ── 池 label 常量 ──────────────────────────────────────
    POOL_LABEL_AGENT = "agentscope.pool.agent"

    # ── 非 adp 会话池常量 ────────────────────────────────
    #: 非 adp 会话（如 deerflow）的池大小固定 1：池按 source
    #: 分流后非 adp 流量走自己的池，单 Pod 排队足够；挂载权限
    #: 不同决定了池必须分离，而非池内混合。
    NON_ADP_POOL_SIZE = 1

    # ── 池大小缓存 TTL（秒）──
    # 短 TTL 平衡热更新时效性与 Redis 往返开销
    POOL_SIZE_CACHE_TTL = 5.0

    # ── 超时与告警阈值（秒）──
    # K8s API 请求默认超时（经由 _patch_api_client_timeout 注入）
    K8S_REQUEST_TIMEOUT_SECS = 30.0
    # workspace 初始化（exec/bootstrap/网关）整体超时：初始化发生在
    # manager 锁内，超时必须退出并释放锁，否则后续所有会话排队等锁。
    # 注意不能短于首次 bootstrap：沙箱 bootstrap 每条命令上限
    # 1800s（apt-get/uv/pip 串行，见框架 _bootstrap_cmd_timeout），
    # 此处只兑底防"无限挂起"，正常耗时靠各细粒度超时（REST 30s、
    # exec 60s、等 Running 120s）保障，不会被本值掩盖。
    WORKSPACE_INIT_TIMEOUT_SECS = 2400.0
    # manager 锁等待告警阈值：超过则打 warning，用于定位锁连坐问题
    MANAGER_LOCK_WARN_SECS = 10.0
    # 池路由（选 Pod + 补建 + 预热网关）整体超时：包含等 Running
    # 120s + 三条 exec（各 60s）+ 健康轮询 15s，最坏累计数百秒，
    # 600s 兑底不误杀正常路径；超时抛异常走失败路径而非永久挂起
    POD_ROUTE_TIMEOUT_SECS = 600.0

    def __init__(
        self,
        *,
        shared_pvc_access_mode: str = "ReadWriteMany",
        # ── 池化 ──
        max_active_pods: int = 5,
        # 全池闲置回收阈值（秒）：agent 的池 Pod 最近一次被路由
        # 命中后超过该时长无任何访问，sweeper 将池全部回收。
        pool_idle_ttl: float = 3600.0,
        # ── 池配置数据源（Redis） ──
        # 连接参数由工厂注入（来自 AppConfig），禁止直读裸环境变量
        redis_host: str = "localhost",
        redis_port: int = 6379,
        # ── 透传给父类的参数 ──
        kubeconfig: str | None = None,
        namespace: str = "agentscope",
        image: str = "python:3.11-slim",
        image_pull_policy: str = "IfNotPresent",
        image_pull_secrets: list[str] | None = None,
        resources: dict[str, Any] | None = None,
        node_selector: dict[str, str] | None = None,
        tolerations: list[dict[str, Any]] | None = None,
        service_account: str | None = None,
        gateway_port: int = 5600,
        extra_pip: list[str] | None = None,
        storage_class: str | None = None,
        storage_size: str = "1Gi",
        env: dict[str, str] | None = None,
        default_mcps: list[Any] | None = None,
        skill_paths: list[str] | None = None,
        ttl: float = 3600.0,
        sweep_interval: float = 300.0,
        delete_pvc_on_close: bool = False,
    ) -> None:
        """初始化共享 PVC 模式的 Manager。

        Args:
            shared_pvc_access_mode (`str`, defaults to ``"ReadWriteMany"``):
                K8s PVC access mode。集群需支持对应存储（NFS/CephFS）。
            （其余参数同 :class:`K8sWorkspaceManager`）
        """
        # 强制 PER_SESSION — 每个 session 一个独立 Pod
        super().__init__(
            isolation=IsolationPolicy.PER_SESSION,
            kubeconfig=kubeconfig,
            namespace=namespace,
            image=image,
            image_pull_policy=image_pull_policy,
            image_pull_secrets=image_pull_secrets,
            resources=resources,
            node_selector=node_selector,
            tolerations=tolerations,
            service_account=service_account,
            gateway_port=gateway_port,
            extra_pip=extra_pip,
            storage_class=storage_class,
            storage_size=storage_size,
            env=env,
            default_mcps=default_mcps,
            skill_paths=skill_paths,
            ttl=ttl,
            sweep_interval=sweep_interval,
            delete_pvc_on_close=delete_pvc_on_close,
        )
        self._shared_pvc_access_mode = shared_pvc_access_mode

        # ── 池化 ──
        self._max_active_pods = max_active_pods
        self._pool_idle_ttl = pool_idle_ttl
        self._redis_host = redis_host
        self._redis_port = redis_port
        # 懒加载长连接 + 池大小短 TTL 缓存，避免每个新会话
        # 都新建/关闭一条 Redis 连接
        self._redis_client: Any = None
        self._redis_lock = asyncio.Lock()
        self._pool_size_cache: dict[str, tuple[float, int]] = {}
        # K8s 客户端（懒加载，用于池管理）
        self._k8s_api_client: Any = None
        self._k8s_v1: Any = None
        self._k8s_lock = asyncio.Lock()

    # ── 覆盖 get_workspace ──────────────────────────────────────

    async def _acquire_lock_with_warning(self, stage: str) -> None:
        """获取 manager 锁；等待超过阈值时打告警。

        锁内包含 ``_build_and_start``（沙箱初始化，可能分钟级）。若某个
        holder 卡死（如 K8s/exec 挂起），后续所有 ``get_workspace`` 会
        在此排队——告警日志是定位"锁连坐"问题的关键埋点。
        """
        try:
            await asyncio.wait_for(
                self._lock.acquire(),
                timeout=self.MANAGER_LOCK_WARN_SECS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: manager lock busy for "
                ">%.0fs (stage=%s) — possible stuck holder blocks all "
                "get_workspace calls",
                self.MANAGER_LOCK_WARN_SECS,
                stage,
            )
            await self._lock.acquire()

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> SharedPvcK8sWorkspace:
        """返回 session-scoped workspace，挂载到 hash 路由的池 Pod。

        Args:
            user_id (`str`): 用户 ID。
            agent_id (`str`): 智能体 ID（用于生成 PVC 名和池 key）。
            session_id (`str`): 会话 ID（用于 hash 路由和 workdir 子目录）。
            workspace_id (`str | None`, optional):
                Stable workspace identifier。``None`` 时自动生成。

        Returns:
            `SharedPvcK8sWorkspace`: 已初始化的 workspace。
        """
        logger.info(
            "SharedPvcK8sWorkspaceManager: get_workspace start "
            "(session=%r, agent=%r, workspace_id=%r)",
            session_id,
            agent_id,
            workspace_id,
        )
        if workspace_id is None:
            workspace_id = self.assign_workspace_id(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )

        # ── 会话来源：adp → 共享区可写 + adp 池；非 adp →
        # 共享区只读 + 独立池（挂载权限不同，池必须分离）。
        # 判定靠 workspace_id 前缀（deerflow 创建会话时写入
        # ``deerflow-{uuid}``）——不依赖 source 字段（框架枚举
        # 不可扩展）；存量无前缀会话一律按 adp 处理。 ──
        is_adp = not workspace_id.startswith(NON_ADP_WORKSPACE_PREFIX)

        # user 级 PVC 名称（hash(user::agent)，不区分 source）
        agent_hash = hashlib.blake2b(
            f"{user_id}::{agent_id}".encode("utf-8"),
            digest_size=8,
        ).hexdigest()
        shared_pvc_name = _k8s_safe_name(agent_hash)

        # agent 级共享 PVC 名称（hash(agent_id)：skills/.mcp）
        agent_shared_pvc_name = _k8s_safe_name(
            hashlib.blake2b(
                agent_id.encode("utf-8"),
                digest_size=8,
            ).hexdigest(),
        )

        # 池 hash：adp 保持现状（与存量池 Pod 一致），非 adp 加
        # source 维度形成独立池（池 Pod 名/标签随之分离；sweeper
        # 按标签分组回收，无需感知 source）
        pool_hash = (
            agent_hash
            if is_adp
            else hashlib.blake2b(
                f"{user_id}::{agent_id}::{NON_ADP_WORKSPACE_PREFIX}".encode(
                    "utf-8",
                ),
                digest_size=8,
            ).hexdigest()
        )

        # ── 缓存查找（session-scoped key） ──
        cached_ws: SharedPvcK8sWorkspace | None = None
        await self._acquire_lock_with_warning("cache lookup")
        try:
            cached = self._cache.get(workspace_id)
            if cached is not None:
                ws, _ = cached
                self._cache[workspace_id] = (ws, time.monotonic())
                cached_ws = ws
        finally:
            self._lock.release()

        if cached_ws is not None:
            # ── 挂载前自检：池 Pod 可能已被任意实例的 sweeper ──
            # 回收/重建，缓存命中必须确认 backend 仍可用，否则
            # 驱逐走重建。多实例部署下其他实例的缓存无法被本
            # 实例 sweeper 清理（其 sweeper 只扫现存 Pod），
            # 自检是跨实例正确性的唯一保障。
            logger.info(
                "SharedPvcK8sWorkspaceManager: cache hit for ws %r; "
                "probing backend aliveness",
                workspace_id,
            )
            if await self._ws_backend_alive(cached_ws):
                return cached_ws  # type: ignore[return-value]
            logger.info(
                "SharedPvcK8sWorkspaceManager: cached ws %r backend "
                "stale; evict and rebuild",
                workspace_id,
            )
            await self._acquire_lock_with_warning("cache evict")
            try:
                cur = self._cache.get(workspace_id)
                if cur is not None and cur[0] is cached_ws:
                    self._cache.pop(workspace_id, None)
            finally:
                self._lock.release()
            await self._safe_close(cached_ws)

        # ── hash 路由选 Pod（不可用顺延，选中后刷新活跃时间） ──
        # 池大小：非 adp 固定 1（独立池，单 Pod 排队）
        pool_size = (
            await self._get_pool_size(agent_id)
            if is_adp
            else self.NON_ADP_POOL_SIZE
        )
        logger.info(
            "SharedPvcK8sWorkspaceManager: pool_size=%d for agent %r "
            "(source=%s); routing session %r",
            pool_size,
            agent_id,
            "adp" if is_adp else NON_ADP_WORKSPACE_PREFIX,
            session_id,
        )
        pod_name = ""
        if pool_size > 0:
            # 整体超时兑底：_route_pod 内含补建 Pod、预热网关（exec）
            # 等分钟级操作，任一环节挂起都会卡死本次 run
            pod_name = await asyncio.wait_for(
                self._route_pod(
                    pool_hash,
                    agent_id,
                    session_id,
                    pool_size,
                    user_pvc_name=shared_pvc_name,
                    agent_shared_pvc_name=agent_shared_pvc_name,
                    shared_read_only=not is_adp,
                ),
                timeout=self.POD_ROUTE_TIMEOUT_SECS,
            )
            logger.info(
                "SharedPvcK8sWorkspaceManager: routed session %r "
                "to pool pod %r (agent=%r)",
                session_id,
                pod_name,
                agent_id,
            )

        # ── 构建/初始化 workspace（锁内；整体超时防止锁被永久持有） ──
        await self._acquire_lock_with_warning("build workspace")
        try:
            cached = self._cache.get(workspace_id)
            if cached is not None:
                ws, _ = cached
                self._cache[workspace_id] = (ws, time.monotonic())
                return ws  # type: ignore[return-value]

            logger.info(
                "SharedPvcK8sWorkspaceManager: building workspace %r "
                "(pod=%r, init timeout=%.0fs)",
                workspace_id,
                pod_name,
                self.WORKSPACE_INIT_TIMEOUT_SECS,
            )
            ws = await asyncio.wait_for(
                self._build_and_start(
                    workspace_id=workspace_id,
                    shared_pvc_name=shared_pvc_name,
                    session_id=session_id,
                    pod_name=pod_name,
                    agent_shared_pvc_name=agent_shared_pvc_name,
                    shared_read_only=not is_adp,
                ),
                timeout=self.WORKSPACE_INIT_TIMEOUT_SECS,
            )
            self._cache[workspace_id] = (ws, time.monotonic())
        finally:
            self._lock.release()
        logger.info(
            "SharedPvcK8sWorkspaceManager: get_workspace done "
            "(session=%r, workdir=%s)",
            session_id,
            ws.workdir,
        )
        return ws  # type: ignore[return-value]

    async def _build_and_start(
        self,
        *,
        workspace_id: str | None,
        shared_pvc_name: str,
        session_id: str,
        pod_name: str = "",
        agent_shared_pvc_name: str = "",
        shared_read_only: bool = False,
    ) -> SharedPvcK8sWorkspace:
        """构造 :class:`SharedPvcK8sWorkspace` 并初始化。

        Args:
            pod_name: 非空表示挂载到池 Pod（hash 路由），空表示
                按需创建 session 级 Pod。
        """
        from agentscope.workspace._utils import DEFAULT_WORKSPACE_INSTRUCTIONS

        ws = SharedPvcK8sWorkspace(
            workspace_id=workspace_id,
            shared_pvc_name=shared_pvc_name,
            session_id=session_id,
            shared_pvc_access_mode=self._shared_pvc_access_mode,
            pod_name=pod_name,
            agent_shared_pvc_name=agent_shared_pvc_name,
            shared_read_only=shared_read_only,
            # 心跳间隔：run 期间每 pool_idle_ttl/2 刷新一次活跃
            # 信号（下限 30s 防止误配导致过频），见 ws 属性说明。
            refresh_heartbeat_interval=max(self._pool_idle_ttl / 2.0, 30.0),
            # ── 透传 Manager 配置 ──
            kubeconfig=self._kubeconfig,
            namespace=self._namespace,
            image=self._image,
            image_pull_policy=self._image_pull_policy,
            image_pull_secrets=self._image_pull_secrets,
            resources=self._resources,
            node_selector=self._node_selector,
            tolerations=self._tolerations,
            service_account=self._service_account,
            gateway_port=self._gateway_port,
            extra_pip=self._extra_pip,
            storage_class=self._storage_class,
            storage_size=self._storage_size,
            delete_pvc_on_close=self._delete_pvc_on_close,
            env=self._env,
            instructions=DEFAULT_WORKSPACE_INSTRUCTIONS,
            default_mcps=self._default_mcps,
            skill_paths=self._skill_paths,
        )
        await ws.initialize()
        return ws

    # ── 池管理 ──────────────────────────────────────────────

    async def _k8s_connect(self) -> None:
        """懒加载 K8s 客户端（用于池 Pod 管理）。"""
        if self._k8s_v1 is not None:
            return
        async with self._k8s_lock:
            if self._k8s_v1 is not None:
                return
            from kubernetes_asyncio import client as k8s_client
            from kubernetes_asyncio import config as k8s_config

            if self._kubeconfig:
                await k8s_config.load_kube_config(
                    config_file=self._kubeconfig,
                )
            else:
                try:
                    k8s_config.load_incluster_config()
                except k8s_config.ConfigException:
                    await k8s_config.load_kube_config()
            self._k8s_api_client = k8s_client.ApiClient()
            _patch_api_client_timeout(
                self._k8s_api_client,
                self.K8S_REQUEST_TIMEOUT_SECS,
            )
            self._k8s_v1 = k8s_client.CoreV1Api(self._k8s_api_client)

    async def _get_pool_size(self, agent_id: str) -> int:
        """获取 agent 维度的池大小。

        优先级：Redis per-agent → .env 全局默认。
        短 TTL 进程内缓存 + 懒加载长连接，避免每个新会话都
        新建/关闭一条 Redis 连接；Redis 异常时静默降级。
        """
        now = time.monotonic()
        cached = self._pool_size_cache.get(agent_id)
        if cached is not None and now - cached[0] < self.POOL_SIZE_CACHE_TTL:
            return cached[1]

        try:
            r = await self._get_redis()
            val = await r.hget(
                f"agentscope:pool:{agent_id}",
                "max_active_pods",
            )
            if val is not None:
                result = int(val)
                self._pool_size_cache[agent_id] = (now, result)
                return result
        except Exception:
            # Redis 不可用 → 不缓存，回退全局默认
            pass
        return self._max_active_pods

    async def _get_redis(self) -> Any:
        """懒加载复用的 Redis 长连接。

        连接参数由工厂注入（来自 AppConfig），与消息总线/存储同源；
        连接异常由 redis-py 自动重连，失败在调用侧捕获降级。
        """
        if self._redis_client is None:
            async with self._redis_lock:
                if self._redis_client is None:
                    import redis.asyncio as aioredis

                    self._redis_client = aioredis.Redis(
                        host=self._redis_host,
                        port=self._redis_port,
                        socket_connect_timeout=2,
                        socket_timeout=2,
                    )
        return self._redis_client

    def invalidate_pool_size(self, agent_id: str) -> None:
        """失效指定 agent 的池大小进程内缓存。

        管理 API（PUT/DELETE /agents/{id}/concurrency）写入 Redis 后
        调用，让新配置立即生效而无需等缓存 TTL 过期。
        """
        self._pool_size_cache.pop(agent_id, None)

    async def _ensure_pool(
        self,
        agent_hash: str,
        agent_id: str,
        primary: int = 0,
        *,
        user_pvc_name: str = "",
        agent_shared_pvc_name: str = "",
        shared_read_only: bool = False,
        pool_size: int | None = None,
    ) -> dict[str, Any]:
        """确保 agent 的池 Pod 齐备，返回 name→Pod 映射。

        补建缺失的 Pod；``primary``（本次 hash 路由命中的索引）
        同步预热/修复网关（新建同步等待、已存在同步探测），
        非 primary 的新建 Pod 后台预热、已存在则保持 lazy
        （下次成为 primary 时再修复）。
        """
        await self._k8s_connect()
        if pool_size is None:
            pool_size = await self._get_pool_size(agent_id)
        existing: dict[str, Any] = {}
        if pool_size <= 0:
            return existing

        # ── 确保 PVC 存在（池 Pod 依赖它们） ──
        # user 级 PVC（session 工作目录）：hash(user::agent)，
        # 与池 hash 独立——非 adp 池的 pool hash 含 source 维度，
        # 不能用于派生 user PVC 名。
        user_pvc_name = user_pvc_name or _k8s_safe_name(agent_hash)
        await self._ensure_shared_pvc(user_pvc_name, agent_id)
        if agent_shared_pvc_name:
            await self._ensure_shared_pvc(agent_shared_pvc_name, agent_id)

        # 查询现有池 Pod
        from kubernetes_asyncio.client.rest import ApiException

        try:
            pods = await self._k8s_v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=(
                    f"{self.POOL_LABEL_AGENT}={agent_hash}"
                ),
            )
        except ApiException as e:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: list pool pods failed: %s",
                e,
            )
            return existing

        for p in pods.items:
            if p.metadata is not None:
                existing[p.metadata.name] = p

        # 补建缺失 Pod：primary 同步预热（路由命中，立即要用），
        # 其余新建后台预热；不可用 Pod（非 Running）不在此删除，
        # 由 sweeper 周期重建，路由顺延保证不阻塞本次访问。
        for i in range(pool_size):
            pod_name = f"as-ws-{agent_hash}-{i}"
            if pod_name in existing:
                if i == primary:
                    try:
                        await self._preheat_pod_gateway(
                            pod_name,
                            shared_read_only=shared_read_only,
                        )
                    except Exception:
                        logger.warning(
                            "SharedPvcK8sWorkspaceManager: gateway "
                            "preheat failed for pool pod %r",
                            pod_name,
                            exc_info=True,
                        )
                continue
            logger.info(
                "SharedPvcK8sWorkspaceManager: creating pool pod %r "
                "(agent=%r)",
                pod_name,
                agent_id,
            )
            await self._create_pool_pod(
                pod_name,
                agent_hash,
                agent_id,
                agent_shared_pvc_name,
                shared_read_only,
                user_pvc_name,
            )
            if i == primary:
                # 新建 Pod：同步等待 Running 并启动网关——创建本就需
                # 等待，预热开销与 Pod 就绪重叠，几乎免费。
                try:
                    await self._preheat_pod_gateway(
                        pod_name,
                        shared_read_only=shared_read_only,
                    )
                except Exception:
                    logger.warning(
                        "SharedPvcK8sWorkspaceManager: gateway preheat "
                        "failed for new pool pod %r",
                        pod_name,
                        exc_info=True,
                    )
            else:
                asyncio.create_task(
                    self._preheat_in_background(
                        pod_name,
                        shared_read_only=shared_read_only,
                    ),
                )
            # 读回最新状态入池：新建 Pod 不在 list 快照里，而
            # _route_pod 靠 existing 判断 Running；不读回会误判
            # “全池无 Running”，把刚建好的 Pod 删掉重建（白删
            # 一轮，删除慢时还会撞删除等待超时）。
            try:
                existing[pod_name] = await self._k8s_v1.read_namespaced_pod(
                    pod_name,
                    self._namespace,
                )
            except ApiException:
                pass
        return existing

    async def _ws_backend_alive(
        self,
        ws: SharedPvcK8sWorkspace,
    ) -> bool:
        """检查 ws 挂载的池 Pod 是否仍可用（Running + 网关健康）。

        仅池模式（``_assigned_pod_name`` 非空）需要检查：池 Pod
        会被任何实例的 sweeper 删除重建，挂载前自检保证缓存命中
        不会返回僵尸 ws。网关探测覆盖"容器重启但 Pod 仍 Running"
        的场景（网关进程丢失）。按需模式的 session Pod 生命周期
        独立（不会被 sweeper 删），直接视为可用。K8s 异常时保守
        返回 True——网络抖动不应导致误驱逐，下轮再自检。
        """
        pod_name = getattr(ws, "_assigned_pod_name", "")
        if not pod_name:
            return True
        try:
            await self._k8s_connect()
        except Exception:
            return True
        from kubernetes_asyncio.client.rest import ApiException

        try:
            pod = await self._k8s_v1.read_namespaced_pod(
                pod_name,
                self._namespace,
            )
        except ApiException as e:
            if e.status == 404:
                return False
            logger.warning(
                "SharedPvcK8sWorkspaceManager: probe pod %r failed: %s",
                pod_name,
                e,
            )
            return True  # 保守：网络抖动不驱逐
        if pod.status is None or pod.status.phase != "Running":
            return False
        # 容器重启后 Pod 仍 Running 但网关进程已丢：探测网关
        return await ws._gateway_healthy()

    @staticmethod
    def _hash_route_index(session_id: str, pool_size: int) -> int:
        """hash(session_id) → 池索引：固定路由，同会话总落同一 Pod。"""
        digest = hashlib.blake2b(
            session_id.encode("utf-8"),
            digest_size=8,
        )
        return int.from_bytes(digest.digest(), "big") % pool_size

    async def _route_pod(
        self,
        agent_hash: str,
        agent_id: str,
        session_id: str,
        pool_size: int,
        *,
        user_pvc_name: str = "",
        agent_shared_pvc_name: str = "",
        shared_read_only: bool = False,
    ) -> str:
        """hash 路由选 Pod：目标不可用顺延到下一个 Running Pod。

        先经 :meth:`_ensure_pool` 补全池（primary=hash 目标，同步
        预热），再从 hash 目标起顺延找第一个 Running Pod 并刷新
        last-active annotation。全池无 Running Pod 时删除重建
        hash 目标 Pod 并等待就绪，兜底保证 run 有可用沙箱。
        周期性的不可用检测与重建主要由 sweeper 负责，这里是
        访问路径上的兜底。
        """
        from kubernetes_asyncio.client.rest import ApiException

        start = self._hash_route_index(session_id, pool_size)
        existing = await self._ensure_pool(
            agent_hash,
            agent_id,
            primary=start,
            user_pvc_name=user_pvc_name,
            agent_shared_pvc_name=agent_shared_pvc_name,
            shared_read_only=shared_read_only,
            pool_size=pool_size,
        )

        for k in range(pool_size):
            i = (start + k) % pool_size
            name = f"as-ws-{agent_hash}-{i}"
            pod = existing.get(name)
            if pod is not None and pod.status is not None and (
                pod.status.phase == "Running"
            ):
                await self._touch_last_active(name)
                return name

        # ── 全池无 Running Pod：重建 hash 目标 Pod 兜底 ──
        name = f"as-ws-{agent_hash}-{start}"
        logger.warning(
            "SharedPvcK8sWorkspaceManager: no running pool pod for "
            "agent %r; rebuilding %r",
            agent_id,
            name,
        )
        try:
            await self._k8s_v1.delete_namespaced_pod(name, self._namespace)
        except ApiException as e:
            if e.status != 404:
                logger.warning(
                    "SharedPvcK8sWorkspaceManager: delete bad pool pod "
                    "%r failed: %s",
                    name,
                    e,
                )
        # 等 Pod 真正消失，避免重建撞 409
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                await self._k8s_v1.read_namespaced_pod(
                    name,
                    self._namespace,
                )
            except ApiException as e:
                if e.status == 404:
                    break
                raise
            await asyncio.sleep(1.0)
        await self._create_pool_pod(
            name,
            agent_hash,
            agent_id,
            agent_shared_pvc_name,
            shared_read_only,
            user_pvc_name,
        )
        await self._preheat_pod_gateway(
            name,
            shared_read_only=shared_read_only,
        )
        await self._touch_last_active(name)
        return name

    async def _touch_last_active(self, pod_name: str) -> None:
        """刷新池 Pod 的 last-active annotation（unix 秒）。

        sweeper 空闲回收的活跃信号。patch 失败静默——annotation
        非关键路径，失败只会让 sweeper 对空闲判定更保守（用
        Pod 创建时间兑底）。
        """
        from kubernetes_asyncio.client.rest import ApiException

        try:
            await self._k8s_v1.patch_namespaced_pod(
                pod_name,
                self._namespace,
                {
                    "metadata": {
                        "annotations": {
                            LAST_ACTIVE_ANNOTATION: str(int(time.time())),
                        },
                    },
                },
            )
        except ApiException:
            pass

    async def _preheat_in_background(
        self,
        pod_name: str,
        *,
        shared_read_only: bool = False,
    ) -> None:
        """后台预热网关：吞异常，避免未捕获任务异常刷日志。"""
        try:
            await self._preheat_pod_gateway(
                pod_name,
                shared_read_only=shared_read_only,
            )
        except Exception:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: background gateway "
                "preheat failed for %r",
                pod_name,
                exc_info=True,
            )

    async def _preheat_pod_gateway(
        self,
        pod_name: str,
        *,
        shared_read_only: bool = False,
    ) -> None:
        """等待池 Pod 运行，并确保网关在 Pod 上健康运行。

        步骤：等 Running → 确保共享目录与默认 ``.mcp`` 落盘 →
        探测网关健康 → 不健康则 pkill + nohup 启动 + 轮询 /health。
        与框架 ``_setup_mcp_gateway`` 的启动方式完全一致（同脚本、
        同配置、同端口），session 挂载时经覆写探测直接复用。
        """
        await self._k8s_connect()
        from kubernetes_asyncio.client.rest import ApiException

        # 1. 等 Running
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try:
                pod = await self._k8s_v1.read_namespaced_pod(
                    pod_name,
                    self._namespace,
                )
            except ApiException as e:
                if e.status == 404:
                    return  # Pod 已被删除，放弃预热
                raise
            phase = pod.status.phase if pod.status else None
            if phase == "Running":
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: pool pod %r not "
                "running in time; skip gateway preheat",
                pod_name,
            )
            return

        backend = K8sBackend(
            api_client=self._k8s_api_client,
            namespace=self._namespace,
            pod_name=pod_name,
            container_name="workspace",
            workdir=POD_WORKDIR,
        )
        shared_dir = f"{POD_WORKDIR}/shared"
        mcp_file = f"{shared_dir}/{DEFAULT_MCP_FILE}"
        gateway_python = (
            f"{GATEWAY_HOME}/{DEFAULT_GATEWAY_VENV}/bin/python"
        )
        gateway_script = f"{GATEWAY_HOME}/{DEFAULT_GATEWAY_SCRIPT}"
        gateway_log = f"{GATEWAY_HOME}/{DEFAULT_GATEWAY_LOG}"

        # 2. 确保共享目录 + 默认 .mcp（与框架 _ensure_workspace_layout
        # 的写入格式一致，agent 级 default_mcps）
        await backend.exec_shell(
            ["mkdir", "-p", shared_dir],
            cwd="/",
            # exec 走 WebSocket，挂起时不抛异常；显式超时防永久等待
            timeout=60.0,
        )
        if not await backend.file_exists(mcp_file):
            payload = json.dumps(
                [
                    m.model_dump(mode="json")
                    for m in (self._default_mcps or [])
                ],
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                await backend.write_file(mcp_file, payload)
            except Exception:
                if shared_read_only:
                    # 只读挂载 + 共享 PVC 尚未由 adp 会话初始化：
                    # 无法落盘 .mcp，网关起不来 → 跳过预热，adp
                    # 会话补齐后自愈。
                    logger.warning(
                        "SharedPvcK8sWorkspaceManager: shared PVC "
                        "not initialized; skip gateway preheat on "
                        "read-only pool pod %r",
                        pod_name,
                    )
                    return
                raise

        # 3. 探测网关
        gateway = GatewayClient(
            backend=backend,
            gateway_port=self._gateway_port,
            timeout=30.0,
            gateway_log_path=gateway_log,
        )
        try:
            if await gateway.health():
                return
        except Exception:
            pass

        # 4. 启动网关（与框架 _setup_mcp_gateway 相同）
        await backend.exec_shell(
            ["sh", "-c", "pkill -f '[_]mcp_gateway_app.py' || true"],
            cwd="/",
            timeout=60.0,
        )
        launch_cmd = (
            f"nohup {shlex.quote(gateway_python)} -u "
            f"{shlex.quote(gateway_script)} "
            f"--config {shlex.quote(mcp_file)} "
            f"--port {self._gateway_port} "
            f"> {shlex.quote(gateway_log)} 2>&1 &"
        )
        await backend.exec_shell(
            ["sh", "-c", launch_cmd],
            cwd="/",
            timeout=60.0,
        )

        # 5. 轮询健康（最多 ~15s）
        deadline = time.monotonic() + 15.0
        delay = 0.1
        while time.monotonic() < deadline:
            try:
                if await gateway.health():
                    logger.info(
                        "SharedPvcK8sWorkspaceManager: preheated "
                        "gateway on pool pod %r",
                        pod_name,
                    )
                    return
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        logger.warning(
            "SharedPvcK8sWorkspaceManager: gateway preheat timeout "
            "on pool pod %r",
            pod_name,
        )

    async def _ensure_shared_pvc(self, pvc_name: str, agent_id: str) -> None:
        """确保共享 PVC 存在，不存在则创建。

        逻辑与 :meth:`SharedPvcK8sWorkspace._ensure_pvc` 一致。
        """
        await self._k8s_connect()
        from kubernetes_asyncio.client.rest import ApiException

        try:
            pvc = await self._k8s_v1.read_namespaced_persistent_volume_claim(
                pvc_name,
                self._namespace,
            )
            if pvc.metadata and pvc.metadata.deletion_timestamp is not None:
                logger.info(
                    "SharedPvcK8sWorkspaceManager: PVC %r is being "
                    "deleted, waiting...",
                    pvc_name,
                )
                # 等待删除完成
                import asyncio as _asyncio

                deadline = _asyncio.get_event_loop().time() + 120.0
                while _asyncio.get_event_loop().time() < deadline:
                    try:
                        await self._k8s_v1.read_namespaced_persistent_volume_claim(
                            pvc_name,
                            self._namespace,
                        )
                    except ApiException as e2:
                        if e2.status == 404:
                            break
                        raise
                    await _asyncio.sleep(2)
                await self._create_shared_pvc(pvc_name, agent_id)
        except ApiException as e:
            if e.status == 404:
                await self._create_shared_pvc(pvc_name, agent_id)
            elif e.status == 409:
                # 并发创建的竞态，PVC 已存在
                return
            else:
                raise

    async def _create_shared_pvc(self, pvc_name: str, agent_id: str) -> None:
        """创建共享 PVC。"""
        await self._k8s_connect()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        access_modes = [self._shared_pvc_access_mode]
        spec_kwargs: dict[str, Any] = {
            "access_modes": access_modes,
            "resources": k8s_client.V1VolumeResourceRequirements(
                requests={"storage": self._storage_size},
            ),
        }
        if self._storage_class is not None:
            spec_kwargs["storage_class_name"] = self._storage_class

        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name=pvc_name,
                namespace=self._namespace,
                labels={
                    "app.kubernetes.io/managed-by": "agentscope",
                    "agentscope.agent_id": _k8s_safe_label(agent_id),
                },
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(**spec_kwargs),
        )
        try:
            await self._k8s_v1.create_namespaced_persistent_volume_claim(
                self._namespace,
                pvc,
            )
        except ApiException as e:
            if e.status == 409:
                # 并发创建竞态，PVC 已由其他实例创建
                return
            raise
        logger.info(
            "SharedPvcK8sWorkspaceManager: created shared PVC %r",
            pvc_name,
        )

    async def _create_pool_pod(
        self,
        pod_name: str,
        agent_hash: str,
        agent_id: str = "",
        agent_shared_pvc_name: str = "",
        shared_read_only: bool = False,
        user_pvc_name: str = "",
    ) -> None:
        """创建单个池 Pod。

        与 session Pod 的区别：
        - label 带 pool 标记（``agentscope.pool.agent``）
        - working_dir 固定为 /workspace
        - 无占用标记——hash 路由下同 Pod 可被多会话并发复用
        - 非 adp 池的共享 PVC 只读挂载（``shared_read_only``）
        """
        await self._k8s_connect()
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio.client.rest import ApiException

        # 容器 spec（与 SharedPvcK8sWorkspace._create_pod 对齐）
        container_env = None
        if self._env:
            container_env = [
                k8s_client.V1EnvVar(name=k, value=v)
                for k, v in self._env.items()
            ]

        container = k8s_client.V1Container(
            name="workspace",
            image=self._image,
            image_pull_policy=self._image_pull_policy,
            command=["sleep", "infinity"],
            working_dir=POD_WORKDIR,
            ports=[
                k8s_client.V1ContainerPort(
                    container_port=self._gateway_port,
                ),
            ],
            resources=(
                k8s_client.V1ResourceRequirements(**self._resources)
                if self._resources
                else None
            ),
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="workspace-data",
                    mount_path=POD_WORKDIR,
                ),
                *(
                    [
                        k8s_client.V1VolumeMount(
                            name="workspace-shared",
                            mount_path=f"{POD_WORKDIR}/shared",
                            read_only=shared_read_only,
                        ),
                    ]
                    if agent_shared_pvc_name
                    else []
                ),
            ],
            env=container_env,
        )

        # user 级 PVC volume（session 工作目录）：与池 hash 独立，
        # 非 adp 池的 pool hash 含 source 维度，不能派生 PVC 名。
        user_pvc_name = user_pvc_name or _k8s_safe_name(agent_hash)
        volumes = [
            k8s_client.V1Volume(
                name="workspace-data",
                persistent_volume_claim=(
                    k8s_client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=user_pvc_name,
                    )
                ),
            ),
            *(
                [
                    k8s_client.V1Volume(
                        name="workspace-shared",
                        persistent_volume_claim=(
                            k8s_client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=agent_shared_pvc_name,
                            )
                        ),
                    ),
                ]
                if agent_shared_pvc_name
                else []
            ),
        ]

        spec_kwargs: dict[str, Any] = {
            "restart_policy": "OnFailure",
            # 沙箱无优雅停机需求（数据在 PVC 已落盘、网关无状态），
            # 快杀让 Pod 删除可靠快速，避免 Terminating 卡满默认
            # 30s grace period 导致删除等待超时。
            "termination_grace_period_seconds": 5,
            "containers": [container],
            "volumes": volumes,
        }
        if self._node_selector:
            spec_kwargs["node_selector"] = self._node_selector
        if self._tolerations:
            spec_kwargs["tolerations"] = [
                k8s_client.V1Toleration(**t) for t in self._tolerations
            ]
        if self._service_account:
            spec_kwargs["service_account_name"] = self._service_account
        if self._image_pull_secrets:
            spec_kwargs["image_pull_secrets"] = [
                k8s_client.V1LocalObjectReference(name=s)
                for s in self._image_pull_secrets
            ]

        pod = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=pod_name,
                namespace=self._namespace,
                labels={
                    "app.kubernetes.io/managed-by": "agentscope",
                    "agentscope.workspace": "true",
                    "agentscope.agent_id": _k8s_safe_label(agent_id),
                    self.POOL_LABEL_AGENT: agent_hash,
                },
            ),
            spec=k8s_client.V1PodSpec(**spec_kwargs),
        )
        try:
            await self._k8s_v1.create_namespaced_pod(self._namespace, pod)
        except ApiException as e:
            if e.status == 409:
                # 并发创建竞态，Pod 已由其他请求创建
                return
            raise

    # ── 池 Pod 生命周期：run 续期 + 不可用重建 + 空闲回收 ─────

    async def _sweep_once(self) -> None:
        """框架 TTL 回收 + run 续期 + 池 Pod 健康/空闲回收。

        1. run 进行中的 ws（``_run_active``）续期访问时间戳——
           否则长 run（> TTL）会被 sweeper close 拆 backend，
           后续工具执行报错；
        2. :meth:`_sweep_pool`：删除不可用池 Pod（下次访问补建），
           全池闲置超 ``pool_idle_ttl`` 时整体回收（PVC 保留）；
        3. 框架 TTL 回收照常。
        """
        now = time.monotonic()
        async with self._lock:
            for wid, (ws, _ts) in list(self._cache.items()):
                if getattr(ws, "_run_active", False):
                    self._cache[wid] = (ws, now)
        await self._sweep_pool()
        await super()._sweep_once()

    async def _sweep_pool(self) -> None:
        """周期扫描全部池 Pod：不可用删除 + 全池空闲回收。

        按 agent（``agentscope.pool.agent`` 标签）分组：

        - 不可用 Pod（非 Running/Pending，或 Running 但主容器长期
          未就绪如 CrashLoopBackOff）→ 删除并驱逐挂载其上的 ws，
          下次访问由 hash 路由/``_ensure_pool`` 补建重建；
        - 全池最近活跃时间（``LAST_ACTIVE_ANNOTATION`` 的最大值，
          无 annotation 用 Pod 创建时间兑底）超过 ``pool_idle_ttl``
          → 删除全部池 Pod（PVC 保留，下次访问懒重建）。
        """
        try:
            await self._k8s_connect()
        except Exception:
            return  # 连不上 K8s：本轮跳过，下轮再试
        from kubernetes_asyncio.client.rest import ApiException

        try:
            pods = await self._k8s_v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=self.POOL_LABEL_AGENT,
            )
        except ApiException as e:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: sweep pool pods failed: %s",
                e,
            )
            return

        by_agent: dict[str, list[Any]] = {}
        for p in pods.items or []:
            if p.metadata is None or p.metadata.labels is None:
                continue
            agent_hash = p.metadata.labels.get(self.POOL_LABEL_AGENT)
            if agent_hash:
                by_agent.setdefault(agent_hash, []).append(p)

        now = time.time()
        for agent_hash, agent_pods in by_agent.items():
            dead: list[str] = []
            last_used = 0.0
            for p in agent_pods:
                meta, status = p.metadata, p.status
                phase = status.phase if status else ""
                created = (
                    meta.creation_timestamp.timestamp()
                    if meta.creation_timestamp is not None
                    else 0.0
                )
                if phase not in ("Running", "Pending"):
                    # Failed/Succeeded/Unknown → 不可用，删除重建
                    dead.append(meta.name)
                    continue
                if phase == "Running" and now - created > 60.0:
                    # CrashLoopBackOff 等：主容器长期未就绪。
                    # 60s 窗口避免误删刚创建、容器尚在启动的 Pod。
                    ready = any(
                        bool(cs.ready)
                        for cs in status.container_statuses or []
                    )
                    if not ready:
                        dead.append(meta.name)
                        continue
                # ── 空闲信号：全池取最近一次活跃 ──
                raw = (meta.annotations or {}).get(
                    LAST_ACTIVE_ANNOTATION,
                )
                try:
                    last_used = max(last_used, float(raw))
                except (TypeError, ValueError):
                    last_used = max(last_used, created)

            # 1. 删除不可用 Pod（下次访问补建 + 预热）
            for pod_name in dead:
                try:
                    await self._k8s_v1.delete_namespaced_pod(
                        pod_name,
                        self._namespace,
                    )
                    logger.info(
                        "SharedPvcK8sWorkspaceManager: deleted "
                        "unhealthy pool pod %r (agent_hash=%r)",
                        pod_name,
                        agent_hash,
                    )
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(
                            "SharedPvcK8sWorkspaceManager: delete "
                            "unhealthy pool pod %r failed: %s",
                            pod_name,
                            e,
                        )
                # 驱逐挂载在死 Pod 上的 ws（backend 已失效，下次
                # 访问重新路由）
                await self._evict_ws_for_pod(pod_name)

            # 2. 全池空闲回收（PVC 保留，下次访问懒重建）
            if last_used > 0 and now - last_used > self._pool_idle_ttl:
                # 本进程有 run 进行中的 ws 挂在该池 → 视为活跃，
                # 跳过本轮回收
                if await self._pool_busy(agent_hash):
                    continue
                for p in agent_pods:
                    await self._evict_ws_for_pod(p.metadata.name)
                    try:
                        await self._k8s_v1.delete_namespaced_pod(
                            p.metadata.name,
                            self._namespace,
                        )
                    except ApiException as e:
                        if e.status != 404:
                            logger.warning(
                                "SharedPvcK8sWorkspaceManager: recycle "
                                "pool pod %r failed: %s",
                                p.metadata.name,
                                e,
                            )
                logger.info(
                    "SharedPvcK8sWorkspaceManager: recycled idle pool "
                    "for agent_hash %r",
                    agent_hash,
                )

    async def _pool_busy(self, agent_hash: str) -> bool:
        """本进程是否有 run 进行中的 ws 挂在该 agent 的池上。"""
        prefix = f"as-ws-{agent_hash}-"
        async with self._lock:
            for ws, _ts in self._cache.values():
                if getattr(ws, "_run_active", False) and (
                    getattr(ws, "_assigned_pod_name", "").startswith(
                        prefix,
                    )
                ):
                    return True
        return False

    async def _evict_ws_for_pod(self, pod_name: str) -> None:
        """驱逐缓存中挂载在指定池 Pod 上的 ws（Pod 即将失效）。

        本进程即时清理：sweeper 删除 Pod 时同步逐出，避免下一轮
        run 命中僵尸 ws 走一遍自检驱逐的往返。跨实例的正确性不
        依赖此处——其他实例的缓存由 get_workspace 挂载前自检
        （``_ws_backend_alive``）兜底。
        """
        evicted: list[Any] = []
        async with self._lock:
            for wid, (ws, _ts) in list(self._cache.items()):
                if getattr(ws, "_assigned_pod_name", "") == pod_name:
                    self._cache.pop(wid, None)
                    evicted.append(ws)
        for ws in evicted:
            await self._safe_close(ws)

    async def cleanup_pool(self, agent_id: str) -> None:
        """删除 agent 对应的所有池资源（Pod + PVC）。

        在删除智能体时调用，清理该 agent 的温池 Pod 和共享 PVC。
        通过 ``agentscope.agent_id`` 标签精确匹配，不依赖 hash。
        不抛异常，仅打日志。
        """
        await self._k8s_connect()
        from kubernetes_asyncio.client.rest import ApiException

        agent_label = f"agentscope.agent_id={_k8s_safe_label(agent_id)}"

        # 1. 删除所有池 Pod
        try:
            pods = await self._k8s_v1.list_namespaced_pod(
                namespace=self._namespace,
                label_selector=(f"{self.POOL_LABEL_AGENT},{agent_label}"),
            )
            for pod in (pods.items or []):
                if pod.metadata is None:
                    continue
                try:
                    await self._k8s_v1.delete_namespaced_pod(
                        pod.metadata.name,
                        self._namespace,
                    )
                    logger.info(
                        "SharedPvcK8sWorkspaceManager: deleted pool pod "
                        "%r (agent=%r)",
                        pod.metadata.name,
                        agent_id,
                    )
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(
                            "SharedPvcK8sWorkspaceManager: delete pool "
                            "pod %r failed: %s",
                            pod.metadata.name,
                            e,
                        )
        except ApiException as e:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: list pool pods for "
                "cleanup failed: %s",
                e,
            )

        # 2. 删除共享 PVC
        try:
            pvcs = await self._k8s_v1.list_namespaced_persistent_volume_claim(
                namespace=self._namespace,
                label_selector=agent_label,
            )
            for pvc in (pvcs.items or []):
                if pvc.metadata is None:
                    continue
                try:
                    await self._k8s_v1.delete_namespaced_persistent_volume_claim(
                        pvc.metadata.name,
                        self._namespace,
                    )
                    logger.info(
                        "SharedPvcK8sWorkspaceManager: deleted shared PVC "
                        "%r (agent=%r)",
                        pvc.metadata.name,
                        agent_id,
                    )
                except ApiException as e:
                    if e.status != 404:
                        logger.warning(
                            "SharedPvcK8sWorkspaceManager: delete shared PVC "
                            "%r failed: %s",
                            pvc.metadata.name,
                            e,
                        )
        except ApiException as e:
            logger.warning(
                "SharedPvcK8sWorkspaceManager: list PVCs for "
                "cleanup failed: %s",
                e,
            )
