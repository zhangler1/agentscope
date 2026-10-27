# -*- coding: utf-8 -*-
"""会话默认权限模式：**一处配置，所有创建入口生效**。

背景
----
``config.yaml`` 的 ``default_permission_mode`` 原先只在 **deerflow 懒创建**
（``deerflow_chat._prepare_session_for_run``）里注入，于是同一部署下不同
入口建出来的会话模式不一致：

======================  ================================================
入口                     默认模式
======================  ================================================
deerflow 懒创建           ``default_permission_mode``（配置值）
``POST /sessions/create``  ``default``（框架字段默认值 → 弹确认卡）
原生 ``POST /sessions/``   ``default``（同上）
======================  ================================================

本模块把默认值收敛到一处：

- :func:`resolve_default_mode`：显式值优先，否则取配置
  （``config.yaml`` / ``BOCOMADP_DEFAULT_PERMISSION_MODE``）；
- :func:`session_state_with_mode`：构造带该模式的 ``AgentState``
  （供新建会话使用）；
- :func:`apply_mode_to_session`：把已有会话的模式改成目标值（读-改-写，
  保留其余 state；已是目标值则跳过）；
- :func:`install_create_session_default_mode`：给**原生**
  ``POST /sessions/`` 前插一条同路径路由（handler 内部调用框架端点，
  再补上默认模式），使其与 ``/sessions/create`` 行为一致。

框架源码零改动 —— 沿用 ``main.py`` 对 ``/agent`` CRUD 的"前插包裹路由"手法。

语义与边界
----------
- **只影响"没显式指定权限"的普通新建会话**：显式传 ``state`` 的链路
  （团队 worker / ``AgentInvite`` 借用 / 调度触发 / deerflow 懒创建）
  一律不受影响；
- 显式传 ``permission_mode`` 时以显式值为准，配置不参与；
- 注入失败（读不到刚建的会话等）只打 warning，**不让建会话失败**。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, status
from fastapi.routing import APIRoute

from agentscope.app._router._schema import (
    CreateSessionRequest,
    CreateSessionResponse,
)
from agentscope.app._router._session import (
    create_session as _framework_create_session,
)
from agentscope.app._service import ResourceAccessService
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_storage,
    get_workspace_manager,
)
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState

from bocomadp.config import get_app_config

# 注意：本模块有 ``from __future__ import annotations``，注解在运行期按
# **模块全局**解析。下面这些类型（请求/响应模型）必须 import 在模块顶层，
# 否则 FastAPI 解析不出 ``body`` 是请求体，会把它当成 query 参数
# （表现为 422 ``loc=["query","body"] Field required``）。

logger = logging.getLogger("bocomadp.session_default_mode")

#: ``app.state`` 上的安装标记（幂等用）。
_INSTALLED_FLAG = "_bocomadp_default_permission_mode_installed"


def resolve_default_mode(
    explicit: PermissionMode | None = None,
) -> PermissionMode:
    """解析新建会话应使用的权限模式。

    显式值优先；未给（``None``）时取 ``config.yaml`` 的
    ``default_permission_mode``（缺省 ``default``，与框架一致）。

    Args:
        explicit (`PermissionMode | None`):
            调用方显式指定的模式；``None`` 表示"未指定"。

    Returns:
        `PermissionMode`: 最终生效的模式。
    """
    if explicit is not None:
        return explicit
    return get_app_config().default_permission_mode


def session_state_with_mode(mode: PermissionMode) -> AgentState:
    """构造新建会话用的 ``AgentState``（只带权限模式，其余取默认）。

    Args:
        mode (`PermissionMode`): 会话初始权限模式。

    Returns:
        `AgentState`: 可直接传给 ``storage.upsert_session(state=...)``。
    """
    return AgentState(permission_context=PermissionContext(mode=mode))


async def apply_mode_to_session(
    storage: StorageBase,
    user_id: str,
    agent_id: str,
    session_id: str,
    mode: PermissionMode,
) -> bool:
    """把 ``session_id`` 的权限模式改成 ``mode``（读-改-写，幂等）。

    只替换 ``state.permission_context.mode``，其余 state（对话上下文、
    working_directories、allow/deny/ask 规则等）原样保留。

    Args:
        storage (`StorageBase`):
            框架 storage。
        user_id (`str`):
            会话归属用户。
        agent_id (`str`):
            会话所属智能体（``get_session`` 定位用）。
        session_id (`str`):
            目标会话。
        mode (`PermissionMode`):
            目标模式。

    Returns:
        `bool`:
            ``True`` = 确实写库；``False`` = 会话不存在或已是目标值
            （幂等，不重复写）。
    """
    record = await storage.get_session(user_id, agent_id, session_id)
    if record is None:
        logger.warning(
            "apply_mode_to_session: session %s not found "
            "(user=%s agent=%s); skip",
            session_id,
            user_id,
            agent_id,
        )
        return False

    if record.state.permission_context.mode == mode:
        return False

    patched_state = record.state.model_copy(
        update={
            "permission_context": record.state.permission_context.model_copy(
                update={"mode": mode},
            ),
        },
    )
    # 用 upsert + session_id 走"更新"分支：config 原样回写、state 替换，
    # 不会新建会话，也不动 created_at。
    await storage.upsert_session(
        user_id=user_id,
        agent_id=agent_id,
        config=record.config,
        state=patched_state,
        session_id=session_id,
    )
    return True


def install_create_session_default_mode(app: Any) -> None:
    """给原生 ``POST /sessions/`` 注入默认权限模式（幂等，可重复调用）。

    做法：往 ``app.router.routes`` **前插**一条同路径同方法的路由，其
    handler 内部调用框架的 ``create_session``（因此可见性校验、凭证校验、
    workspace 分配、以及 bocomadp 对 ``_ensure_credential_exists`` 的
    patch 全部照旧），随后把结果会话的模式补成
    :func:`resolve_default_mode` 的值。

    Starlette 按 ``routes`` 顺序取第一个匹配，所以前插即生效；框架那条
    原路由被遮蔽但保留（便于回退/对比），OpenAPI 里只会出现我们这条。

    为什么不用改 framework 源码或包装 storage：``upsert_session`` 同时
    承担"更新"语义（``state=None`` 时更新分支不动 state），在 storage 层
    注入需要额外区分"新建 vs 更新"，容易误覆盖已有会话的 state；前插路由
    的作用面只限这一个端点。

    Args:
        app (`Any`):
            FastAPI 应用（``create_app()`` 的返回值，尚未挂到 ``/api``）。
    """
    if getattr(app.state, _INSTALLED_FLAG, False):
        return

    async def create_session_with_default_mode(
        body: CreateSessionRequest,
        user_id: str = Depends(get_current_user_id),
        storage: StorageBase = Depends(get_storage),
        workspace_manager: WorkspaceManagerBase = Depends(
            get_workspace_manager,
        ),
        access: ResourceAccessService = Depends(
            get_resource_access_service,
        ),
    ) -> CreateSessionResponse:
        """与框架 ``create_session`` **完全同契约**，额外补默认权限模式。

        Args:
            body (`CreateSessionRequest`):
                与原生一致（agent / workspace / 模型配置）。
            user_id (`str`): Injected authenticated user ID.
            storage (`StorageBase`): Injected storage backend.
            workspace_manager (`WorkspaceManagerBase`): Injected manager.
            access (`ResourceAccessService`): Injected access service.

        Returns:
            `CreateSessionResponse`: ``{"session_id": "..."}``（与原生一致）。
        """
        response = await _framework_create_session(
            body=body,
            user_id=user_id,
            storage=storage,
            workspace_manager=workspace_manager,
            access=access,
        )
        try:
            mode = resolve_default_mode(None)
            changed = await apply_mode_to_session(
                storage,
                user_id,
                body.agent_id,
                response.session_id,
                mode,
            )
            logger.info(
                "create_session: session=%s default permission mode=%s "
                "(applied=%s)",
                response.session_id,
                mode.value,
                changed,
            )
        except Exception:  # noqa: BLE001 —— 注入失败不影响建会话本身
            logger.warning(
                "create_session: default permission mode injection "
                "failed for session %s",
                response.session_id,
                exc_info=True,
            )
        return response

    app.router.routes.insert(
        0,
        APIRoute(
            # 与框架 session_router 的 prefix="/sessions" + "/" 完全同路径。
            "/sessions/",
            create_session_with_default_mode,
            methods=["POST"],
            response_model=CreateSessionResponse,
            status_code=status.HTTP_201_CREATED,
            summary="Create a new session (default permission mode injected)",
            name="create_session_with_default_mode",
        ),
    )
    setattr(app.state, _INSTALLED_FLAG, True)
    logger.info(
        "installed default-permission-mode wrapper for POST /sessions/",
    )


__all__ = [
    "apply_mode_to_session",
    "install_create_session_default_mode",
    "resolve_default_mode",
    "session_state_with_mode",
]
