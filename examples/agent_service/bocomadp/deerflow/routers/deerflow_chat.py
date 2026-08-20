# -*- coding: utf-8 -*-
"""DeerFlow 风格 SSE 路由（threads/runs 资源模型）。

对齐 deer-flow 2.0 ``backend/app/gateway/routers/thread_runs.py`` 的 4 个
端点，但执行引擎复用原生 ``ChatService``（配置与原生 ``/chat/`` 完全
一致——agent 构建、模型、工具、审计中间件、HITL 全部同源）：

- ``POST /api/deerflow/threads/{tid}/runs/stream``  创建 run + SSE 流式
- ``POST /api/deerflow/threads/{tid}/runs/wait``    创建 run + 阻塞至完成
- ``GET  /api/deerflow/threads/{tid}/runs/{rid}/stream``  join 已有 run（回放 + live）
- ``POST /api/deerflow/threads/{tid}/runs/{rid}/cancel``  取消（映射原生 session 级 interrupt）

设计要点（方案决策①④⑤）：

- thread_id == session_id（同一资源）；run_id 由 RunManager 预生成，
  ``Content-Location`` 头可提前填充。
- 并发 409 复用 ``ChatRunRegistry.spawn`` + 分布式锁；cancel 映射原生
  ``ChatService.interrupt``（一个 session 至多一个 run，run 级 == session 级）。
- 断线默认 ``on_disconnect=cancel``：检测到断线后调用原生 interrupt，
  停止后台任务不再消耗模型额度；``continue`` 时仅断开订阅，run 继续。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextvars import Token
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agentscope.app.deps import (
    get_chat_run_registry,
    get_chat_service,
    get_storage,
    get_workspace_manager,
)
from agentscope.app._manager import ChatRunRegistry
from agentscope.app._service import ChatService
from agentscope.app.storage import (
    ChatModelConfig,
    SessionConfig,
    StorageBase,
)
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.credential import CredentialFactory
from agentscope.event import (
    ConfirmResult,
    ExternalExecutionResultEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg, TextBlock

from bocomadp.config import load_models_from_yaml
from bocomadp.credential.ellm import ELLMCredential
from bocomadp.logging.trace_context import run_id_context
from bocomadp.routers.uploads import download_urls_to_session

from ..bridge import BusBridge
from ..credentials import (
    DEFAULT_CREDENTIAL_OWNER,
    credential_cls_for_entry,
    credential_kwargs_for_entry,
    default_credential_id,
    is_deerflow_credential_id,
    user_credential_id,
)
from ..auth_context import (
    reset_resolved_auth,
    resolve_auth_params,
    set_resolved_auth,
)
from ..custom_params import (
    load_custom_params_from_workspace,
    reset_custom_params,
    save_custom_params_to_workspace,
    set_custom_params,
)
from ..deps import (
    get_bridge,
    get_deerflow_user_id,
    get_run_manager,
)
from ..protocol import (
    END_SENTINEL,
    EVENT_CUSTOM,
    EVENT_ERROR,
    EVENT_MESSAGES,
    StreamEvent,
    format_sse,
)
from ..runs import RunManager, RunRecord, RunStatus

logger = logging.getLogger(__name__)

# LangGraph 消息 type → 原生 Msg.role（前端 SDK 固定发 human）。
_ROLE_MAP = {"human": "user", "ai": "assistant", "system": "system"}


class _HumanInputResponseMarker:
    """human 消息携带 ``human_input_response`` 的标记。

    前端确认卡片（HumanInputCard）应答通过一条 ``hide_from_ui`` 的
    human 消息提交，``additional_kwargs.human_input_response`` 携带应答
    载荷；后端据此构造 :class:`UserConfirmResultEvent` 续跑（Case B）。
    保留原始消息 dict 以便匹配失败时回退为普通消息处理。
    """

    def __init__(self, response: dict, raw: dict) -> None:
        self.response = response
        self.raw = raw


def _extract_human_input_response(raw: dict) -> dict | None:
    """提取 human 消息 ``additional_kwargs.human_input_response``。

    仅识别 ``kind == "human_input_response"`` 的载荷（与前端
    [human-input.ts](file:///home/llm/zhangle/agentscope-workspace/deer-flow-2.0/frontend/src/core/messages/human-input.ts)
    的 :func:`parseHumanInputResponse` 对齐），其余消息返回 None。
    """
    if str(raw.get("type", "")) != "human":
        return None
    additional_kwargs = raw.get("additional_kwargs")
    if not isinstance(additional_kwargs, dict):
        return None
    response = additional_kwargs.get("human_input_response")
    if isinstance(response, dict) and response.get(
        "kind",
    ) == "human_input_response":
        return response
    return None


def _langgraph_message_to_msg(raw: dict) -> Msg:
    """LangGraph 消息 dict（type/content）→ 原生 Msg。

    type: human→user / ai→assistant / system→system；content 支持字符串
    或块数组（仅保留 text 块）；additional_kwargs 等扩展字段忽略。
    """
    role = _ROLE_MAP.get(str(raw.get("type", "human")))
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported LangGraph message type {raw.get('type')!r}.",
        )
    content = raw.get("content")
    if isinstance(content, str):
        blocks = [TextBlock(text=content)]
    elif isinstance(content, list):
        blocks = [
            TextBlock(text=block["text"])
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    else:
        blocks = []
    return Msg(name=role, role=role, content=blocks)


def _convert_input(raw: Any) -> Any:
    """SDK input（dict / LangGraph 消息）→ 原生 ChatRequest.input 形态。

    优先检测前端确认卡片应答（human 消息携带 human_input_response），
    返回 :class:`_HumanInputResponseMarker` 供路由层构造确认事件；其余
    消息按原逻辑转换。
    """
    if raw is None or isinstance(raw, Msg):
        return raw
    if isinstance(raw, (UserConfirmResultEvent, ExternalExecutionResultEvent)):
        return raw
    if isinstance(raw, list):
        return [_convert_input(item) for item in raw]
    if isinstance(raw, dict):
        response = _extract_human_input_response(raw)
        if response is not None:
            return _HumanInputResponseMarker(response, raw)
        messages = raw.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    response = _extract_human_input_response(message)
                    if response is not None:
                        return _HumanInputResponseMarker(response, message)
            return [_langgraph_message_to_msg(m) for m in messages]
        if isinstance(raw.get("type"), str) and "content" in raw:
            return _langgraph_message_to_msg(raw)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unsupported input dict: expected {'messages': [...]} or "
                f"a single LangGraph message, got keys {list(raw)}."
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported input type {type(raw).__name__}.",
    )


def _msg_to_human_chunk(msg: Msg) -> dict[str, Any]:
    """原生 Msg（用户输入）→ LangGraph human 消息 chunk。

    与 threads.py ``_msg_to_langgraph`` 同构（type/id/content 块数组）；
    id 取 Msg.id——与 ``ChatService.run`` 持久化进 storage 的是同一实例，
    刷新/join 后前端按 id 去重不会出现第二条。
    """
    blocks = [
        {"type": "text", "text": block.text}
        for block in msg.content
        if isinstance(block, TextBlock)
    ]
    return {
        "type": "human",
        "id": msg.id,
        "content": blocks if blocks else "",
    }


def _collect_human_chunks(input_msg: Any) -> list[dict[str, Any]]:
    """从转换后的输入提取用户消息 chunk（Case A 回显；事件续跑无）。"""
    msgs = (
        input_msg
        if isinstance(input_msg, list)
        else [input_msg]
        if isinstance(input_msg, Msg)
        else []
    )
    return [_msg_to_human_chunk(m) for m in msgs]


async def _load_human_chunks(
    storage: StorageBase,
    user_id: str,
    thread_id: str,
) -> list[dict[str, Any]]:
    """join 场景回显：从 storage 读最近用户消息 → human chunks。

    断线重连（SDK joinStream）时前端乐观消息尚未清理，而回放日志无
    human 事件；补发保证 SDK 的 ``values.messages`` 含用户消息，前端
    human 计数增长后才会清理乐观消息（与 create 路径同因）。
    """
    try:
        messages, _ = await storage.list_messages(
            user_id,
            thread_id,
            limit=50,
        )
    except Exception:  # noqa: BLE001 —— join 回显尽力而为，失败不影响连接
        logger.exception(
            "deerflow: failed to load human chunks for thread %s",
            thread_id,
        )
        return []
    return [_msg_to_human_chunk(m) for m in messages if m.role == "user"]

deerflow_router = APIRouter(prefix="/deerflow/threads", tags=["deerflow"])

# 注意：本路由挂载在 main.py 的 /api 子应用下，对外路径为
# /api/deerflow/threads/...；deer-flow 前端旧路径 /api/threads/... 由
# nginx 网关 rewrite 兼容。


# ── 请求模型 ──────────────────────────────────────────────────────────


class CreateRunRequest(BaseModel):
    """创建 run 的请求体。

    兼容 LangGraph SDK 的调用契约（前端经 ``useStream`` 发起）：

    - ``assistant_id`` 接受但忽略（前端适配已废弃）；``agent_id`` 必填，
      缺失时 400。
    - ``input`` 接受 SDK 的 ``{"messages": [...]}`` / 单条消息 dict，
      转换后等价于原生 ``ChatRequest.input``。
    - ``session_id`` 必填且必须等于 thread_id；deer-flow 扩展参数
      （``stream_mode`` / ``multitask_strategy``）接受但忽略——本方案
      固定流模式与 reject 并发策略（裁剪项 1/2）。
    """

    agent_id: str | None = Field(
        default=None,
        description="Agent ID（与原生 /chat/ 一致），必填；缺失时 400。",
    )
    assistant_id: str | None = Field(
        default=None,
        description="接受但忽略：前端适配已废弃，不再作为 agent_id 别名。",
    )
    session_id: str | None = Field(
        default=None,
        description="原生 session id，必填且必须等于 thread_id（两者同一资源）。",
    )
    input: (
        Msg
        | list[Msg]
        | UserConfirmResultEvent
        | ExternalExecutionResultEvent
        | dict
        | None
    ) = Field(
        default=None,
        description="输入消息。兼容 LangGraph SDK 的 "
        "``{'messages': [...]}`` / 单条消息 dict，转换后等价于原生 "
        "ChatRequest.input。",
    )
    stream_mode: list[str] | str | None = Field(
        default=None,
        description="接受但忽略：本方案固定 messages + custom 流。",
    )
    multitask_strategy: str = Field(
        default="reject",
        description="接受但忽略：恒为 reject（409 语义由原生注册表保证）。",
    )
    on_disconnect: Literal["cancel", "continue"] = Field(
        default="cancel",
        description="客户端断线后行为：cancel 立即中断 run（默认，对齐 "
        "deer-flow）；continue 仅断开订阅、run 继续执行。",
    )
    custom_params: dict[str, Any] | None = Field(
        default=None,
        description="请求级自定义参数（空间码等），注入后台 run 任务，"
        "由工具中间件强制覆盖模型传参。",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "LangGraph SDK 固定携带的 context（前端 overrides）。"
            "接受但忽略：原生 ChatRequest 无请求级配置通道，模型名"
            "改由 custom_params.llm_model_name 传递。"
        ),
    )
    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "LangGraph SDK 的 RunnableConfig。接受但忽略：原生 "
            "ChatRequest 无请求级配置通道。"
        ),
    )


# ── 内部辅助 ──────────────────────────────────────────────────────────


def _resolve_session_id(thread_id: str, body: CreateRunRequest) -> str:
    """thread_id 与 session_id 同一资源；session_id 必填且必须一致。"""
    if body.session_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="session_id is required and must equal thread_id.",
        )
    if body.session_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"session_id={body.session_id!r} must equal thread_id "
                f"{thread_id!r} (thread_id == session_id)."
            ),
        )
    return thread_id


def _resolve_requested_model_name(
    custom_params: dict[str, Any] | None = None,
) -> str:
    """解析请求级模型名（单一通道：custom_params.llm_model_name）。

    对齐 deer-flow lead_agent 的 runtime_model_name。调用方保证传入
    ELLM 需要的模型名，本函数不做校验/映射，直接信任。原生
    ChatRequest 无请求级模型名通道，context / config 等 LangGraph
    SDK 字段不再作为模型名来源（接受但忽略）。

    缺失返回空串（调用方回退全局 active provider / config.yaml
    条目解析）。
    """
    value = (custom_params or {}).get("llm_model_name")
    return str(value).strip() if value else ""


def _resolve_agent_id(body: CreateRunRequest) -> str:
    """agent_id 必填（与原生 /chat/ 一致）；assistant_id 接受但忽略。"""
    if not body.agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_id is required; assistant_id is accepted "
            "but ignored.",
        )
    return body.agent_id


async def _check_agent_id(
    storage: StorageBase,
    user_id: str,
    agent_id: str,
) -> str:
    """按原生可见性语义校验 agent_id；不可见一律 404。

    agent 全部存于原生 storage（config.yaml seed 机制已废弃），校验
    只查 storage（原生 resolve_agent 语义）：该 user_id 下可见 →
    原样采用；否则 404——不再有 seed 名单或回退默认 agent 之类的
    配置依赖。
    """
    if await storage.get_agent(user_id, agent_id) is not None:
        return agent_id
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"Agent {agent_id!r} not found for user {user_id!r}; "
            "agents are resolved from native storage only."
        ),
    )


async def _resolve_chat_model_config(
    storage: StorageBase,
    request: Request,
    user_id: str,
    hint: str = "",
) -> ChatModelConfig | None:
    """把 config.yaml 的模型条目解析为原生 ChatModelConfig。

    优先按请求级模型名（``hint``）匹配 config.yaml 模型条目
    （model_name / provider_id 双键，对齐 deer-flow ``_resolve_model_name``
    的语义）；未命中告警后回退全局 active provider（ProviderManager）。
    匹配到条目后按「用户优先、默认复制」解析 credential：用户维度凭证
    （``deerflow-<user_id>-<provider_id>``）已存在则直接引用，否则从
    default 用户默认凭证（``deerflow-default-<provider_id>``）复制参数
    入库，两者都不存在时回退条目参数。无可用条目时返回 None（由原生
    404 报错兜底，不阻断请求）。
    """

    # 全局 active provider 兜底（config.yaml agents 种子机制已移除，
    # 不再有 agent 级模型绑定来源）
    provider_id = ""
    model_name = ""
    pm = getattr(request.app.state, "provider_manager", None)
    active = pm.get_active_model() if pm is not None else None
    if active is not None:
        provider_id = active.provider_id
        model_name = active.model_name

    # 请求级模型名优先：按 model_name / provider_id 双键匹配 config.yaml
    # 模型条目（deer-flow 前端的模型名可能对应条目 model_name 或
    # provider_id）；未命中告警并沿用已解析的 active provider。
    # 约定 credential id 直传（/api/deerflow/models 返回的 id）则直接
    # 解析出 provider_id，等价于命中该 config 条目。
    entry = None
    static_hit = False
    credential_provider = None
    resolved_model = ""  # 动态命中（ELLM）时的真实模型 ID；空 = 未命中
    if hint:
        credential_provider = is_deerflow_credential_id(hint, user_id)
        if credential_provider:
            provider_id = credential_provider
            logger.info(
                "deerflow: credential id %r resolved to provider %r "
                "(user=%s).",
                hint,
                provider_id,
                user_id,
            )
        else:
            entry = next(
                (
                    candidate
                    for candidate in load_models_from_yaml()
                    if candidate.model_name == hint
                    or candidate.provider_id == hint
                ),
                None,
            )
            if entry is not None:
                static_hit = True
                provider_id = entry.provider_id
                model_name = entry.model_name or model_name
            else:
                logger.warning(
                    "deerflow: model %r not found in config.yaml; "
                    "fallback to active provider %r (user=%s).",
                    hint,
                    provider_id,
                    user_id,
                )
    if entry is None:
        entry = next(
            (
                candidate
                for candidate in load_models_from_yaml()
                if candidate.provider_id == provider_id
            ),
            None,
        )
    # 动态模型路由（ELLM 内网模型）：hint 非约定 credential id、也未
    # 静态命中双键匹配时，把它当作真实模型 ID 路由到 ELLM 类型条目，
    # 运行时覆盖 model 字段（对齐 deer-flow dynamic_model 语义）。
    # 目标条目：兜底解析出的条目本身为 ELLM（active 指向 ELLM
    # provider）→ 直接命中；条目缺失且 provider_id 为空 → 取
    # config.yaml 中唯一 ELLM 条目（多个告警取第一个）；否则维持
    # 现状（hint 丢弃）。
    dynamic_hit = False
    if hint and not static_hit and credential_provider is None:
        if entry is not None:
            entry_cls = credential_cls_for_entry(entry)
            if entry_cls is not None and issubclass(entry_cls, ELLMCredential):
                dynamic_hit = True
        elif not provider_id:
            ellm_entries = []
            for candidate in load_models_from_yaml():
                candidate_cls = credential_cls_for_entry(candidate)
                if candidate_cls is not None and issubclass(
                    candidate_cls,
                    ELLMCredential,
                ):
                    ellm_entries.append(candidate)
            if len(ellm_entries) > 1:
                logger.warning(
                    "deerflow: multiple ELLM entries %s; using %r as "
                    "dynamic model target.",
                    [c.provider_id for c in ellm_entries],
                    ellm_entries[0].provider_id,
                )
            if ellm_entries:
                entry = ellm_entries[0]
                dynamic_hit = True
        if dynamic_hit:
            provider_id = entry.provider_id
            resolved_model = hint
            logger.info(
                "deerflow: hint %r routed to ELLM provider %r as "
                "dynamic model (user=%s).",
                hint,
                entry.provider_id,
                user_id,
            )
    if entry is None:
        logger.warning(
            "deerflow: no model entry for provider %r; session created "
            "without chat_model_config.",
            provider_id,
        )
        return None

    credential_cls = credential_cls_for_entry(entry)
    if credential_cls is None:
        logger.warning(
            "deerflow: unknown provider_type %r; session created without "
            "chat_model_config.",
            entry.provider_type,
        )
        return None

    # api_key 空的条目不可用（无密钥可调用）；ELLM 例外——key 动态获取
    # （首次调用由 EllmKeyRefresher 立即刷新写入）。
    if not entry.api_key and not issubclass(credential_cls, ELLMCredential):
        logger.warning(
            "deerflow: model entry %r has no api_key; session created "
            "without chat_model_config.",
            entry.provider_id,
        )
        return None

    # 用户优先、默认复制（见 credentials.py 模块注释）：
    # - 用户维度凭证已存在 → 直接引用（重复则使用本用户的，用户改过
    #   的 api_key 等生效）；
    # - 不存在 → 从 default 用户默认凭证复制参数入库（默认凭证入库后
    #   参数源不再是 config.yaml 直读）；
    # - default 凭证亦不存在（未入库/被删）→ 回退条目参数。
    credential_id = user_credential_id(user_id, entry.provider_id)
    own = await storage.get_credential(user_id, credential_id)
    if own is None:
        default_rec = await storage.get_credential(
            DEFAULT_CREDENTIAL_OWNER,
            default_credential_id(entry.provider_id),
        )
        copied = False
        if default_rec is not None:
            try:
                source = CredentialFactory.from_dict(
                    dict(default_rec.data or {}),
                )
                source.id = credential_id
                await storage.upsert_credential(user_id, source)
                copied = True
            except Exception:  # noqa: BLE001 —— 复制失败回退条目参数
                logger.warning(
                    "deerflow: failed to copy default credential %s "
                    "to user %s; falling back to config.yaml entry.",
                    default_rec.id,
                    user_id,
                    exc_info=True,
                )
        if not copied:
            await storage.upsert_credential(
                user_id,
                credential_cls(
                    **credential_kwargs_for_entry(
                        entry,
                        credential_id,
                        credential_cls,
                        model=resolved_model
                        or entry.model_name
                        or entry.provider_id,
                    ),
                ),
            )
        logger.info(
            "deerflow: credential %s %s for user %s.",
            credential_id,
            "copied from default" if copied else "created from entry",
            user_id,
        )

    return ChatModelConfig(
        type=entry.provider_type,
        credential_id=credential_id,
        model=resolved_model or entry.model_name or entry.provider_id,
        parameters=entry.parameters,
    )


async def _ensure_session(
    storage: StorageBase,
    workspace_manager,
    request: Request,
    user_id: str,
    agent_id: str,
    session_id: str,
    model_name_hint: str = "",
) -> None:
    """原生 ChatService.run 要求 session 已存在且带模型配置；缺失时补齐。

    workspace_id 由 workspace_manager 的隔离策略分配（与原生 /session/
    创建路径一致）；chat_model_config 优先按请求级模型名匹配 config.yaml
    条目，未命中回退全局 active provider（ProviderManager），保证默认
    会话也能在原生链路上真实调用模型。
    会话已存在时：配置缺失则回填；请求显式携带模型名（``model_name_hint``
    非空）且解析出的 (type, credential_id, model) 与现状不一致时才更新
    （per-run 模型切换）；未携带模型名则保持既有配置不动——原生接口
    建好的会话（如绑定 ELLM 凭证）不被 config.yaml 全局 active
    provider 静默覆盖。
    """
    existing = await storage.get_session(user_id, agent_id, session_id)
    if (
        existing is not None
        and existing.config.chat_model_config is not None
        and not model_name_hint
    ):
        # 会话已存在且带模型配置、本次未显式指定模型名 → 保持既有
        # 配置不动：原生接口建好的会话（如绑定 ELLM 凭证）不能被
        # config.yaml 的全局 active provider 静默覆盖。
        return
    model_config = await _resolve_chat_model_config(
        storage,
        request,
        user_id,
        model_name_hint,
    )
    if existing is not None:
        existing_config = existing.config.chat_model_config
        if model_config is not None and (
            existing_config is None
            or (
                existing_config.type,
                existing_config.credential_id,
                existing_config.model,
            )
            != (
                model_config.type,
                model_config.credential_id,
                model_config.model,
            )
        ):
            await storage.upsert_session(
                user_id=user_id,
                agent_id=agent_id,
                config=existing.config.model_copy(
                    update={"chat_model_config": model_config},
                ),
                session_id=session_id,
            )
            logger.info(
                "deerflow: %s chat_model_config for session %s "
                "(agent=%s, model=%s).",
                "backfilled" if existing_config is None else "updated",
                session_id,
                agent_id,
                model_config.model,
            )
        return
    workspace_id = workspace_manager.assign_workspace_id(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    config_kwargs: dict[str, Any] = {"workspace_id": workspace_id}
    if model_config is not None:
        config_kwargs["chat_model_config"] = model_config
    await storage.upsert_session(
        user_id=user_id,
        agent_id=agent_id,
        config=SessionConfig(**config_kwargs),
        session_id=session_id,
    )
    logger.info(
        "deerflow: session %s auto-created for agent %s (user=%s).",
        session_id,
        agent_id,
        user_id,
    )


async def _resolve_custom_params(
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
    user_id: str,
    agent_id: str,
    session_id: str,
    requested: dict[str, Any] | None,
) -> dict[str, Any]:
    """解析本次 run 的 custom_params（对齐 deer-flow ``_resolve_custom_params``）。

    请求携带 custom_params → 落盘到会话绑定的 workspace（后续请求可
    回退恢复）并直接采用；未携带 → 从会话 workspace 的落盘文件回退
    加载（HITL 确认续跑等场景空间码约束持续生效）。

    workspace 解析复用与 skill_router 相同的 DB 持久化路径
    （``session.config.workspace_id``），任意隔离策略下都精确；落盘 /
    读盘非致命——workspace 不可用或文件缺失时降级为空 dict，不阻断
    run 创建。
    """
    if requested is not None:
        # 运行时覆盖——落盘供后续请求恢复（对齐 deer-flow 保存语义）
        session_record = await storage.get_session(
            user_id,
            agent_id,
            session_id,
        )
        if session_record is not None:
            try:
                workspace = await workspace_manager.get_workspace(
                    user_id,
                    agent_id,
                    session_id,
                    session_record.config.workspace_id,
                )
                await save_custom_params_to_workspace(
                    workspace,
                    session_id,
                    requested,
                )
            except Exception:  # noqa: BLE001 —— 落盘失败不阻断 run 创建
                logger.warning(
                    "deerflow: workspace unavailable for session %s; "
                    "custom_params persist skipped",
                    session_id,
                    exc_info=True,
                )
        return requested

    # 无运行时值——尝试从会话 workspace 回退加载
    session_record = await storage.get_session(
        user_id,
        agent_id,
        session_id,
    )
    if session_record is None:
        return {}
    try:
        workspace = await workspace_manager.get_workspace(
            user_id,
            agent_id,
            session_id,
            session_record.config.workspace_id,
        )
        loaded = await load_custom_params_from_workspace(
            workspace,
            session_id,
        )
    except Exception:  # noqa: BLE001 —— 读盘失败降级为空 dict
        logger.warning(
            "deerflow: workspace unavailable for session %s; "
            "custom_params fallback skipped",
            session_id,
            exc_info=True,
        )
        return {}
    return loaded or {}


async def _download_additional_urls(
    body: CreateRunRequest,
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
) -> None:
    """下载 custom_params.additional_urls 到会话 uploads 目录（仅副作用）。

    ``custom_params: {additional_urls: ["http://.../a.png", ...]}`` 中的
    地址是 OSS / HTTP(S) 直链，需在 run 启动前下载并保存到会话 uploads
    目录（与 ``POST /files/upload`` 同链路：落盘 + 图片 base64 / 文档
    .md + uploads DB 记录，下游工具与 ``<context name="files">`` 立即可见）。

    本函数不改变任何参数：custom_params（含 additional_urls）由调用方
    原样交给 ``_resolve_custom_params`` **整体落盘** ``custom_params.json``，
    便于事后查看历史传参。下载仅在请求显式携带 additional_urls 时触发
    一次；回退加载路径（请求未携带时从落盘文件恢复）不会再次触发下载，
    故持久化不会导致重复下载。
    """
    if not body.custom_params or not body.custom_params.get("additional_urls"):
        return
    raw = body.custom_params.get("additional_urls")
    urls = (
        [u.strip() for u in raw if isinstance(u, str) and u.strip()]
        if isinstance(raw, list)
        else []
    )
    if urls:
        downloaded = await download_urls_to_session(
            user_id,
            agent_id,
            session_id,
            urls,
            storage,
            workspace_manager,
        )
        if downloaded:
            logger.info(
                "deerflow: saved %d additional_url file(s) for thread %s.",
                len(downloaded),
                session_id,
            )


def _set_run_auth_contexts(
    params: dict[str, Any],
) -> dict[str, Token]:
    """spawn 前注入认证上下文：ResolvedAuth + guwp token 联动。

    对齐 deer-flow ``_resolve_auth_params``：把 custom_params 的认证
    字段解析为 :class:`ResolvedAuth` 写入 ContextVar，供 run 任务内的
    工具后端经 :func:`get_resolved_auth` 读取。

    ``guwp_token`` 同时联动 agent-factory 的 ``_current_token``
    ContextVar——run 任务内 ``_resolve_session_token``（main.py）读取
    它并持久化到 session token store，技能下载等工具直接可用。

    返回各 ContextVar 的 reset token，spawn 完成后逐项 reset
    （``asyncio.create_task`` 已复制上下文快照，reset 不影响后台任务）。
    """
    tokens: dict[str, Token] = {}
    tokens["auth"] = set_resolved_auth(resolve_auth_params(params))
    guwp_token = str(params.get("guwp_token") or "")
    if guwp_token:
        from bocomadp.tools.agent_factory_tools import _current_token

        tokens["guwp"] = _current_token.set(guwp_token)
    return tokens


def _reset_run_auth_contexts(tokens: dict[str, Token]) -> None:
    """恢复 :func:`_set_run_auth_contexts` 注入的 ContextVar。"""
    try:
        guwp_token = tokens.get("guwp")
        if guwp_token is not None:
            from bocomadp.tools.agent_factory_tools import _current_token

            _current_token.reset(guwp_token)
    except Exception:  # noqa: BLE001 —— reset 失败仅告警
        logger.warning(
            "deerflow: failed to reset guwp token context (non-fatal)",
            exc_info=True,
        )
    try:
        auth_token = tokens.get("auth")
        if auth_token is not None:
            reset_resolved_auth(auth_token)
    except Exception:  # noqa: BLE001 —— reset 失败仅告警
        logger.warning(
            "deerflow: failed to reset auth context (non-fatal)",
            exc_info=True,
        )


async def _build_user_confirm_event(
    storage: StorageBase,
    user_id: str,
    agent_id: str,
    session_id: str,
    response: dict,
) -> UserConfirmResultEvent | None:
    """前端确认卡片应答 → UserConfirmResultEvent（Case B 续跑）。

    按 ``request_id == "confirm-{tool_call.id}"`` 匹配会话中 ASKING 状态
    的待确认工具调用；value 映射：

    - ``confirm`` → 同意（rules=None）
    - ``reject`` → 拒绝
    - ``confirm_always`` → 同意且把工具调用携带的 suggested_rules 一并
      传入（落入 allow_rules，后续同前缀命令免确认）

    匹配不到待确认工具调用（会话状态已变化/重放请求）时返回 None，
    由调用方回退为普通消息处理，不阻断。
    """
    request_id = str(response.get("request_id", ""))
    value = str(response.get("value", ""))
    session_record = await storage.get_session(
        user_id,
        agent_id,
        session_id,
    )
    if session_record is None:
        logger.warning(
            "deerflow: confirm response %r dropped, session %s not found",
            request_id,
            session_id,
        )
        return None
    agent_record = await storage.get_agent(user_id, agent_id)
    agent_name = (
        agent_record.data.name if agent_record is not None else agent_id
    )
    awaiting = session_record.state.get_awaiting_tool_calls(agent_name)
    tool_call = next(
        (tc for tc in awaiting if f"confirm-{tc.id}" == request_id),
        None,
    )
    if tool_call is None:
        logger.warning(
            "deerflow: confirm response %r matches no awaiting tool call "
            "in session %s",
            request_id,
            session_id,
        )
        return None
    confirmed = value != "reject"
    rules = (
        list(tool_call.suggested_rules)
        if value == "confirm_always" and tool_call.suggested_rules
        else None
    )
    return UserConfirmResultEvent(
        reply_id=session_record.state.reply_id,
        confirm_results=[
            ConfirmResult(
                confirmed=confirmed,
                tool_call=tool_call,
                rules=rules,
            ),
        ],
    )


def _spawn_run(
    run_manager: RunManager,
    chat_run_registry: ChatRunRegistry,
    chat_service: ChatService,
    user_id: str,
    session_id: str,
    agent_id: str,
    input_msg: Any,
) -> tuple[RunRecord, asyncio.Task]:
    """RunManager 记账 + 原生注册表 spawn；任何冲突 → 409。"""
    try:
        record = run_manager.create_or_reject(
            user_id,
            session_id,
            agent_id,
            native_registry=chat_run_registry,
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e

    try:
        # spawn 前绑定 run_id：asyncio.create_task 复制调用方上下文，
        # 后台 agent 任务内的事件日志（MODEL_*/TOOL_*）因此能带上 run_id
        # 字段，与 RunManager 记账/SSE 订阅链路关联。
        with run_id_context(record.run_id):
            task = chat_run_registry.spawn(
                chat_service.run(
                    user_id,
                    session_id,
                    agent_id,
                    input_msg,
                    run_id=record.run_id,
                ),
                session_id=session_id,
                name=f"deerflow-run:{record.run_id}",
            )
    except RuntimeError as e:
        run_manager.set_status(
            record.run_id,
            RunStatus.ERROR,
            error="session busy",
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e

    run_manager.set_status(record.run_id, RunStatus.RUNNING)

    def _on_done(t: asyncio.Task) -> None:
        # 只在仍 RUNNING 时落终态：cancel 端点可能已置 interrupted，
        # 不覆盖；ChatService.run 内部吞异常，错误已由 REPLY_END(error)
        # 事件表达，此处仅兜底。
        rec = run_manager.get(record.run_id)
        if rec is None or rec.status != RunStatus.RUNNING:
            return
        if t.cancelled():
            run_manager.mark_finished(record.run_id, RunStatus.INTERRUPTED)
        elif t.exception() is not None:
            run_manager.mark_finished(
                record.run_id,
                RunStatus.ERROR,
                error=str(t.exception()),
            )
        else:
            run_manager.mark_finished(record.run_id, RunStatus.SUCCESS)

    task.add_done_callback(_on_done)
    return record, task


def _sse_generator(
    bridge: BusBridge,
    chat_service: ChatService,
    run_manager: RunManager,
    request: Request,
    user_id: str,
    session_id: str,
    agent_id: str,
    run_id: str,
    on_disconnect: str,
    run_finished: bool = False,
    human_chunks: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[str, None]:
    """回放 + live 订阅 → deer-flow 帧；断线/异常/结束均收敛为帧。

    Args:
        run_finished (`bool`, optional):
            run 已确认结束（join 路径由调用方从 RunManager / 原生注册表
            推断）；为真时回放未遇 end 立即收尾，避免 live 空等挂死连接。
        human_chunks (`list[dict]`, optional):
            创建 run 时回显的用户输入 chunk（LangGraph human 消息，id 与
            storage 持久化一致）。SDK 依赖 messages 事件把用户消息并入
            ``values.messages``——缺失时前端 human 计数不增长，乐观消息
            永不清理，界面出现两条用户输入（"问题显示两次"）。
    """

    async def _gen() -> AsyncGenerator[str, None]:
        # HITL park 标志：收到确认请求帧（on_require_confirm）后置真。
        # park 是回复的正常终点（end 哨兵由 formatter 补发），此时
        # interrupt 会走"锁已释放"分支，enqueue UserInterruptEvent 把
        # ASKING 的待确认工具调用全部标记 interrupted——摧毁等待用户
        # 确认的状态，确认应答将永远匹配不到工具调用。故 finally 里
        # 仅断线（未 park）才 interrupt。
        hitl_parked = False
        try:
            # 首帧回显用户输入（先于一切总线事件，保证 values.messages
            # 顺序 [human, ai, ...]；id 与 storage 一致，刷新后去重不重复）
            for chunk in human_chunks or []:
                yield format_sse(
                    StreamEvent(
                        id="",
                        event=EVENT_MESSAGES,
                        data=[chunk, {"langgraph_node": "user"}],
                    ),
                )
            async for evt in bridge.subscribe_run(
                session_id,
                run_id,
                # 空串 Last-Event-ID 头视为无游标，避免 log_read 收到 '' 崩溃
                last_event_id=request.headers.get("Last-Event-ID") or None,
                run_finished=run_finished,
            ):
                if evt is END_SENTINEL:
                    # 状态同步落定（end 帧与 done 回调之间存在毫秒级窗口，
                    # 提前落定可避免紧随其后的新 run 误判 409）；error 帧
                    # 已先行落定 ERROR，此处不覆盖。
                    _finish_if_running(run_manager, run_id, RunStatus.SUCCESS)
                    yield format_sse(evt)
                    return
                if evt.event == EVENT_ERROR:
                    _finish_if_running(run_manager, run_id, RunStatus.ERROR)
                if (
                    evt.event == EVENT_CUSTOM
                    and isinstance(evt.data, dict)
                    and evt.data.get("type") == "on_require_confirm"
                ):
                    hitl_parked = True
                if await request.is_disconnected():
                    break
                yield format_sse(evt)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 流内异常收敛为 error + end
            logger.exception(
                "deerflow stream failed for run %s: %s",
                run_id,
                e,
            )
            yield format_sse(
                StreamEvent(
                    id="",
                    event=EVENT_ERROR,
                    data={"message": str(e), "name": "StreamError"},
                ),
            )
            yield format_sse(END_SENTINEL)
        finally:
            if on_disconnect == "cancel" and not hitl_parked:
                try:
                    await chat_service.interrupt(
                        user_id,
                        session_id,
                        agent_id,
                    )
                except LookupError:
                    # run 已完成、session 已清理时的正常情形，不必告警
                    pass
                except Exception:  # noqa: BLE001 —— 兜底中断失败仅记日志
                    logger.exception(
                        "deerflow: interrupt on disconnect failed for "
                        "session %s run %s",
                        session_id,
                        run_id,
                    )

    return _gen()


def _finish_if_running(
    run_manager: RunManager,
    run_id: str,
    status: RunStatus,
) -> None:
    """run 仍活跃时落定终态；已结束（cancel/其他订阅者先行）不覆盖。"""
    rec = run_manager.get(run_id)
    if rec is not None and rec.active:
        run_manager.mark_finished(run_id, status)


def _streaming_response(
    thread_id: str,
    run_id: str,
    generator: AsyncGenerator[str, None],
) -> StreamingResponse:
    """组装 StreamingResponse：deer-flow 协议头 + Content-Location。"""
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # LangGraph SDK 用正则从该头提取 run id（对齐 deer-flow）。
            "Content-Location": f"/api/deerflow/threads/{thread_id}/runs/{run_id}",
        },
    )


# ── 端点 1：创建 run + 流式 ──────────────────────────────────────────


@deerflow_router.post(
    "/{thread_id}/runs/stream",
    summary="Create a run and stream events via SSE",
)
async def create_run_stream(
    thread_id: str,
    body: CreateRunRequest,
    request: Request,
    user_id: str = Depends(get_deerflow_user_id),
    run_manager: RunManager = Depends(get_run_manager),
    bridge: BusBridge = Depends(get_bridge),
    chat_service: ChatService = Depends(get_chat_service),
    chat_run_registry: ChatRunRegistry = Depends(get_chat_run_registry),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> StreamingResponse:
    """创建 run（后台任务 = 原生 ChatService.run）并立即 SSE 流式。

    首个消费者自带回放语义：即使后台任务已开始发布事件，订阅建立前
    的事件仍会从 Redis Stream replay log 补齐（见 bridge 两阶段设计）。
    会话（thread）懒创建：thread_id 对应的 session 不存在时按 agent_id
    自动建库，保证原生 ChatService.run 的 session 前置条件成立。
    """
    session_id = _resolve_session_id(thread_id, body)
    agent_id = await _check_agent_id(
        storage,
        user_id,
        _resolve_agent_id(body),
    )
    converted = _convert_input(body.input)
    # 请求级模型名（custom_params.llm_model_name，唯一通道），在
    # custom_params 落盘/回退之前解析——首次建会话时 workspace 尚不存在，
    # 直接用请求携带值即可（对齐 deer-flow 每轮携带语义）。
    model_name_hint = _resolve_requested_model_name(body.custom_params)
    await _ensure_session(
        storage,
        workspace_manager,
        request,
        user_id,
        agent_id,
        session_id,
        model_name_hint,
    )
    if isinstance(converted, _HumanInputResponseMarker):
        # 前端确认卡片应答：构造 UserConfirmResultEvent 续跑（Case B）。
        # 原 human 消息仍回显 chunk——前端依赖 messages 事件中的 human
        # 消息让 human 计数增长、清理乐观消息（hide_from_ui 不可见）。
        confirm_event = await _build_user_confirm_event(
            storage,
            user_id,
            agent_id,
            session_id,
            converted.response,
        )
        human_input_msg = _langgraph_message_to_msg(converted.raw)
        if confirm_event is None:
            # 匹配不到待确认工具调用（状态已变化/重放请求）：按普通
            # 消息兜底处理，不阻断对话。
            input_msg = human_input_msg
        else:
            input_msg = confirm_event
        human_chunks = _collect_human_chunks(human_input_msg)
    else:
        input_msg = converted
        human_chunks = _collect_human_chunks(input_msg)
    # spawn 前处理 custom_params：请求携带 additional_urls 时先下载到
    # 会话 uploads 目录；随后 custom_params（含 additional_urls）整体
    # 落盘到 custom_params.json（便于查看历史传参），未携带则从会话
    # workspace 回退加载，reset 不影响已创建的后台 run 任务。
    await _download_additional_urls(
        body,
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    resolved_params = await _resolve_custom_params(
        storage,
        workspace_manager,
        user_id,
        agent_id,
        session_id,
        body.custom_params,
    )
    ctx_token = set_custom_params(resolved_params)
    auth_tokens = _set_run_auth_contexts(resolved_params)
    try:
        record, _task = _spawn_run(
            run_manager,
            chat_run_registry,
            chat_service,
            user_id,
            session_id,
            agent_id,
            input_msg,
        )
    finally:
        _reset_run_auth_contexts(auth_tokens)
        reset_custom_params(ctx_token)
    logger.info(
        "deerflow: run %s created for thread %s (agent=%s).",
        record.run_id,
        thread_id,
        agent_id,
    )
    return _streaming_response(
        thread_id,
        record.run_id,
        _sse_generator(
            bridge,
            chat_service,
            run_manager,
            request,
            user_id,
            session_id,
            agent_id,
            record.run_id,
            body.on_disconnect,
            human_chunks=human_chunks,
        ),
    )


# ── 端点 2：创建 run + 阻塞等待 ──────────────────────────────────────


@deerflow_router.post(
    "/{thread_id}/runs/wait",
    summary="Create a run and block until it completes",
)
async def create_run_wait(
    thread_id: str,
    body: CreateRunRequest,
    request: Request,
    user_id: str = Depends(get_deerflow_user_id),
    run_manager: RunManager = Depends(get_run_manager),
    chat_service: ChatService = Depends(get_chat_service),
    chat_run_registry: ChatRunRegistry = Depends(get_chat_run_registry),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> dict[str, Any]:
    """创建 run 并阻塞至后台任务完成，返回 run 终态。"""
    session_id = _resolve_session_id(thread_id, body)
    agent_id = await _check_agent_id(
        storage,
        user_id,
        _resolve_agent_id(body),
    )
    input_msg = _convert_input(body.input)
    # 同 create_run_stream：先解析请求级模型名（custom_params 唯一通道）
    # 再懒建会话。
    model_name_hint = _resolve_requested_model_name(body.custom_params)
    await _ensure_session(
        storage,
        workspace_manager,
        request,
        user_id,
        agent_id,
        session_id,
        model_name_hint,
    )
    # 同 create_run_stream：请求携带 additional_urls 时先下载到会话
    # uploads 目录；随后 custom_params（含 additional_urls）整体落盘
    # 到 custom_params.json（便于查看历史传参），未携带则从会话
    # workspace 回退加载，reset 不影响已创建的后台 run 任务
    # （create_task 复制 ContextVar 上下文）。
    await _download_additional_urls(
        body,
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )
    resolved_params = await _resolve_custom_params(
        storage,
        workspace_manager,
        user_id,
        agent_id,
        session_id,
        body.custom_params,
    )
    ctx_token = set_custom_params(resolved_params)
    auth_tokens = _set_run_auth_contexts(resolved_params)
    try:
        record, task = _spawn_run(
            run_manager,
            chat_run_registry,
            chat_service,
            user_id,
            session_id,
            agent_id,
            input_msg,
        )
    finally:
        _reset_run_auth_contexts(auth_tokens)
        reset_custom_params(ctx_token)
    try:
        await task
    except asyncio.CancelledError:
        pass
    rec = run_manager.get(record.run_id) or record
    return {
        "run_id": rec.run_id,
        "thread_id": thread_id,
        "status": rec.status.value,
        "error": rec.error,
    }


# ── 端点 3：join 已有 run ────────────────────────────────────────────


@deerflow_router.get(
    "/{thread_id}/runs/{run_id}/stream",
    summary="Join an existing run's stream (replay + live)",
)
async def join_run_stream(
    thread_id: str,
    run_id: str,
    request: Request,
    cancel_on_disconnect: bool = Query(default=False),
    user_id: str = Depends(get_deerflow_user_id),
    run_manager: RunManager = Depends(get_run_manager),
    bridge: BusBridge = Depends(get_bridge),
    chat_service: ChatService = Depends(get_chat_service),
    chat_run_registry: ChatRunRegistry = Depends(get_chat_run_registry),
    storage: StorageBase = Depends(get_storage),
) -> StreamingResponse:
    """订阅一个已有 run：先回放（``Last-Event-ID`` 断点续传）再 live。

    run 不在 RunManager 记账内时放行（兼容原生 ``/chat/`` 触发的 run
    ——其 run_id 可从 session 事件流的 ``run_id`` 字段获得）；session
    不匹配或不存在时 404。

    已确认结束的 run（记账落定终态，或原生注册表无活跃任务）回放后
    立即收尾——否则 live 阶段只剩心跳帧，连接永不关闭，前端
    ``isStreaming`` 卡死（“请等待当前响应完成”）。

    ``cancel_on_disconnect`` 对齐 SDK joinStream 的 query 参数（默认
    ``0``）：为真时断线取消 run；为假（默认）时断线仅断开订阅。
    """
    record = run_manager.get(run_id)
    if record is not None:
        if record.session_id != thread_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Run {run_id} not found in thread {thread_id}.",
            )
        agent_id = record.agent_id
        existing = await storage.get_session(
            user_id,
            agent_id,
            thread_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Thread '{thread_id}' not found.",
            )
        # 记账已落定终态 → 无活跃事件流，回放后立即收尾
        run_finished = not record.active
    else:
        # 原生链路触发的 run：无记账记录，不校验（回放无事件则挂起等 live）。
        agent_id = ""
        # 原生注册表也无活跃任务 → run 必然已结束；仍进 live 只会空等
        # 心跳帧，SSE 连接永不关闭，故回放后立即收尾。
        native_task = chat_run_registry.get(thread_id)
        run_finished = native_task is None or native_task.done()
        logger.debug(
            "deerflow: joining unregistered run %s on thread %s "
            "(likely triggered via native /chat/); run_finished=%s.",
            run_id,
            thread_id,
            run_finished,
        )

    human_chunks = await _load_human_chunks(storage, user_id, thread_id)
    return _streaming_response(
        thread_id,
        run_id,
        _sse_generator(
            bridge,
            chat_service,
            run_manager,
            request,
            user_id,
            thread_id,
            agent_id,
            run_id,
            "cancel" if cancel_on_disconnect else "continue",
            run_finished=run_finished,
            human_chunks=human_chunks,
        ),
    )


# ── 端点 4：run 详情（SDK runs.get 终态预检）────────────────────────


@deerflow_router.get(
    "/{thread_id}/runs/{run_id}",
    summary="Get details of a run",
)
async def get_run_detail(
    thread_id: str,
    run_id: str,
    run_manager: RunManager = Depends(get_run_manager),
) -> dict[str, Any]:
    """返回 run 详情（对齐 LangGraph SDK ``runs.get`` 契约）。

    前端 SDK ``reconnectOnMount`` 时先调本端点做终态预检
    （``shouldSkipReconnect``）：run 已落定终态则跳过 joinStream 直接
    走 ``onSuccess``，避免在已结束的 run 上 join 空等心跳帧、
    ``isStreaming`` 永不翻转（“一直 thinking”）。

    未记账的 run（原生 ``/chat/`` 触发）返回 404——SDK 捕获后回退到
    joinStream，与真实 deer-flow 行为一致。
    """
    record = run_manager.get(run_id)
    if record is None or record.session_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found in thread {thread_id}.",
        )
    return {
        "run_id": record.run_id,
        "thread_id": record.session_id,
        "assistant_id": record.agent_id,
        "status": record.status.value,
        "error": record.error,
        "metadata": {},
        "kwargs": {},
        "multitask_strategy": "reject",
        "created_at": "",
        "updated_at": "",
    }


# ── 端点 5：取消 ─────────────────────────────────────────────────────


@deerflow_router.post(
    "/{thread_id}/runs/{run_id}/cancel",
    summary="Cancel a running or pending run",
)
async def cancel_run(
    thread_id: str,
    run_id: str,
    request: Request,
    user_id: str = Depends(get_deerflow_user_id),
    run_manager: RunManager = Depends(get_run_manager),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict[str, Any]:
    """取消 run：RunManager 状态置 interrupted + 原生 session 级 interrupt。

    安全性由原生分布式锁保证（一个 session 至多一个 run），run 级与
    session 级取消等价；join 方收到 ``REPLY_END(INTERRUPTED)`` 翻译的
    ``end`` 哨兵后立即断开。
    """
    record = run_manager.get(run_id)
    if record is None or record.session_id != thread_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found.",
        )
    if record.active:
        run_manager.mark_finished(run_id, RunStatus.INTERRUPTED)
    try:
        await chat_service.interrupt(
            user_id,
            thread_id,
            record.agent_id,
        )
    except LookupError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    return {
        "run_id": run_id,
        "status": RunStatus.INTERRUPTED.value,
    }


__all__ = ["deerflow_router"]
