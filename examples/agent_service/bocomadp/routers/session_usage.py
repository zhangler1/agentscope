# -*- coding: utf-8 -*-
"""会话相关扩展接口（token 用量查询 + 创建会话并自动绑定凭证）。

Endpoint
--------
``GET  /sessions/{session_id}/usage?agent_id=xxx&user_id=xxx``
``GET  /sessions/limit``
``POST /sessions/create``
``POST /sessions/update``

    - usage：返回 ``input_tokens`` / ``output_tokens`` / ``message_count``
      （聚合会话内全部已落库消息）。
    - limit：分页返回某智能体的会话 id 列表（直连 DB，COUNT + LIMIT）。
    - create：创建会话并**自动注入该智能体绑定的 ELLM 凭证**——请求体与
      原生 ``POST /api/sessions`` 一致，唯独 ``chat_model_config`` 只传
      ``model`` / ``parameters``，``type`` 与 ``credential_id`` 由后端补齐
      （``credential_id`` 取自 ``agent_credential`` 表按 ``agent_id`` 的绑定，
      ``type`` 固定为 ``bocom_ellm_credential``），最终落库结构与原生接口
      完全一致。
    - update：更新已有会话的模型配置，同样**自动注入该智能体绑定的
      ELLM 凭证**。语义与原生 ``PATCH /api/sessions/{session_id}`` 一致
      （省略字段 = 不改，显式传 ``null`` = 清空），差异同样只在
      ``chat_model_config`` 为精简形态（``type`` / ``credential_id`` 由后端
      按 ``agent_id`` 的绑定补齐）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agentscope._utils._common import _generate_id
from agentscope.app._router._schema import CreateSessionResponse
from agentscope.app._service import ResourceAccessService
from agentscope.app.access import ResourceKind
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_storage,
    get_workspace_manager,
)
from agentscope.app.storage import (
    ChatModelConfig,
    CredentialRecord,
    SessionConfig,
    SessionKnowledgeConfig,
    SessionRecord,
    StorageBase,
    TTSModelConfig,
)
from agentscope.app.workspace_manager import WorkspaceManagerBase

from bocomadp.routers.agent_credential import get_agent_credential_id

logger = logging.getLogger("bocomadp.session_usage")

session_usage_router = APIRouter(
    prefix="/sessions",
    tags=["session-usage"],
)

#: 本接口注入的凭证类型（与 ``bocomadp/credential/ellm.py`` 的
#: ``ELLMCredential.type`` 保持一致）。
ELLM_CREDENTIAL_TYPE = "bocom_ellm_credential"


def _as_payload_dict(raw: Any) -> dict[str, Any]:
    """把驱动返回的 ``payload`` 统一成 dict。

    裸 ``text()`` SQL 会丢掉列的类型信息，SQLAlchemy 的 JSON 结果处理器
    不生效：MySQL / OceanBase 的 aiomysql 驱动把 ``JSON`` 列以**字符串**
    返回（Postgres 的 asyncpg 则自动解码成 dict）。``None`` / 无法解析 /
    非 dict 的形态一律退化为空 dict，交由调用方按缺省值处理。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            logger.warning("sessions.payload is not valid JSON; using {}")
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


@session_usage_router.get(
    "/{session_id}/usage",
    summary="Get cumulative token usage for a session",
)
async def get_session_usage(
    session_id: str,
    agent_id: str = Query(default="default", description="Agent id"),
    user_id: str = Query(default="default", description="User id"),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    """Sum token usage across all messages in a session.

    Iterates through the session's message list via paginated
    ``list_messages``, accumulating ``usage.input_tokens`` and
    ``usage.output_tokens`` from every :class:`Msg` that has them.
    """
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend not available",
        )

    # Check session ownership
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    total_input = 0
    total_output = 0
    message_count = 0
    before: str | None = None
    batch_limit = 200

    while True:
        messages, has_more = await storage.list_messages(
            user_id,
            session_id,
            limit=batch_limit,
            before=before,
        )
        for msg in messages:
            message_count += 1
            u = getattr(msg, "usage", None)
            if u is not None:
                total_input += getattr(u, "input_tokens", 0) or 0
                total_output += getattr(u, "output_tokens", 0) or 0

        if not has_more or not messages:
            break
        # Move cursor to continue pagination
        before = messages[0].id

    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "message_count": message_count,
    }


@session_usage_router.get(
    "/limit",
    summary="Paginated session ids for an agent (direct DB query)",
)
async def list_session_ids_paginated(
    agent_id: str = Query(default="default", description="Agent id"),
    user_id: str = Depends(get_current_user_id),
    page: int = Query(default=1, ge=1, description="Page number, starts at 1"),
    page_size: int = Query(
        default=20,
        ge=1,
        le=200,
        description="Number of items per page (1-200)",
    ),
) -> dict:
    """Return a paginated list of session records for an agent.

    The payload shape mirrors the framework's ``GET /sessions/``
    (``ListSessionsResponse``): a ``sessions`` array of full
    :class:`SessionRecord` objects, plus ``total``. On top of that we
    also expose ``agent_id``, ``page``, ``page_size`` and ``has_more``
    for the client-side pagination.

    Queries the ``sessions`` table directly via the shared async engine
    (same DB URL as the framework storage, reuse ``pool_config``'s
    lazy-loaded engine to avoid opening an extra connection pool), so
    pagination is pushed down to the database (COUNT + LIMIT/OFFSET)
    instead of loading every session through ``storage.list_sessions``.
    """
    from sqlalchemy import text

    from agentscope.app.storage import SessionRecord
    from bocomadp.pool_config import _get_engine

    engine = await _get_engine()

    # Total number of matching sessions
    async with engine.connect() as conn:
        total = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM sessions "
                    "WHERE user_id = :user_id AND agent_id = :agent_id",
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )
        ).scalar_one()

    offset = (page - 1) * page_size

    # Current page of session records, newest-first
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, created_at, updated_at, user_id, agent_id, "
                    "source, source_schedule_id, team_id, payload "
                    "FROM sessions "
                    "WHERE user_id = :user_id AND agent_id = :agent_id "
                    "ORDER BY created_at DESC "
                    "LIMIT :limit OFFSET :offset",
                ),
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "limit": page_size,
                    "offset": offset,
                },
            )
        ).all()

    # Reconstruct full SessionRecord objects the same way the SQL storage
    # mapper does: merge the promoted columns back into ``payload`` and
    # let ``model_validate`` fire the record's validators.
    #
    # 注意：``text()`` 裸 SQL 绕过 SQLAlchemy 的 JSON 结果处理器，``payload``
    # （MySQL/OceanBase 的 JSON 列）会以**字符串**回到这里（Postgres/asyncpg
    # 则由驱动自动解码）。必须先解码成 dict，否则 ``dict("...")`` 会抛
    # ``ValueError: dictionary update sequence element #0 has length 1``。
    sessions: list[dict] = []
    for row in rows:
        # text() 原生 SQL 不经过 ORM 的 JSON 类型处理：PG 原生 JSON 列
        # 返回 dict，但 MySQL（本项目实际为 TEXT 存储）返回 JSON 字符串，
        # 需与 memory/store.py 保持一致，两种形态都要兼容。
        raw_payload = row.payload
        obj: dict = (
            json.loads(raw_payload)
            if isinstance(raw_payload, str) and raw_payload
            else dict(raw_payload or {})
        )
        obj["id"] = row.id
        obj["created_at"] = row.created_at
        obj["updated_at"] = row.updated_at
        obj["user_id"] = row.user_id
        obj["agent_id"] = row.agent_id
        obj["source"] = row.source
        obj["source_schedule_id"] = row.source_schedule_id
        obj["team_id"] = row.team_id
        sessions.append(
            SessionRecord.model_validate(obj).model_dump(mode="json")
        )

    return {
        "sessions": sessions,
        "total": total,
        "agent_id": agent_id,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(sessions) < total,
    }


# ---------------------------------------------------------------------------
# 创建会话：自动绑定该智能体的 ELLM 凭证
# ---------------------------------------------------------------------------


class SessionChatModelInput(BaseModel):
    """创建会话时传入的**精简**模型配置（只给 ``model`` / ``parameters``）。

    相比原生 :class:`ChatModelConfig` 少了 ``type`` 与 ``credential_id``：
    二者由后端补齐 —— ``credential_id`` 按 ``agent_id`` 从
    ``agent_credential`` 绑定表查出，``type`` 固定为
    ``bocom_ellm_credential``。
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(
        min_length=1,
        description="模型名，如 'deepseek-v4-flash'。",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="模型参数（原样写入 ChatModelConfig.parameters）。",
    )


class CreateSessionWithCredentialRequest(BaseModel):
    """``POST /sessions/create`` 请求体。

    字段与原生 ``CreateSessionRequest`` 一致，唯一差异是
    ``chat_model_config`` 为精简形态（无 ``type`` / ``credential_id``）。
    其余模型配置仍按原生形态传入。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(description="会话所属智能体。")
    workspace_id: str | None = Field(
        default=None,
        description=(
            "可选显式 workspace 绑定；省略时与原生一致，由 "
            "``WorkspaceManagerBase.assign_workspace_id`` 按隔离策略分配。"
        ),
    )
    name: str | None = Field(
        default=None,
        description="会话显示名；省略则用当前时间。",
    )
    chat_model_config: SessionChatModelInput = Field(
        description="主模型配置（精简形态，type / credential_id 由后端补齐）。",
    )
    fallback_chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description="备用模型配置（原生形态）；省略则不设置。",
    )
    tts_model_config: TTSModelConfig | None = Field(
        default=None,
        description="TTS 模型配置（原生形态）；省略则不设置。",
    )
    knowledge_config: SessionKnowledgeConfig | None = Field(
        default=None,
        description="会话知识库挂载配置（原生形态）；省略则不挂载。",
    )


class UpdateSessionWithCredentialRequest(BaseModel):
    """``POST /sessions/update`` 请求体（PATCH 语义：省略 = 不改）。

    字段与原生 ``UpdateSessionRequest`` 一致（除 ``permission_mode`` 外），
    唯一差异是 ``chat_model_config`` 为精简形态（无 ``type`` /
    ``credential_id``，由后端按 ``agent_id`` 的绑定凭证补齐）。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(description="要更新的会话 ID。")
    agent_id: str = Field(description="会话所属智能体。")
    name: str | None = Field(
        default=None,
        description="新会话名；省略则不改。",
    )
    chat_model_config: SessionChatModelInput | None = Field(
        default=None,
        description=(
            "主模型配置（精简形态，type / credential_id 由后端按该智能体的"
            "绑定凭证补齐）。省略则保持原值；显式传 null 则清空。"
        ),
    )
    fallback_chat_model_config: ChatModelConfig | None = Field(
        default=None,
        description=(
            "备用模型配置（原生形态）；省略则不改，显式传 null 则清空。"
        ),
    )
    tts_model_config: TTSModelConfig | None = Field(
        default=None,
        description=(
            "TTS 模型配置（原生形态）；省略则不改，显式传 null 则清空。"
        ),
    )
    knowledge_config: SessionKnowledgeConfig | None = Field(
        default=None,
        description=(
            "会话知识库挂载配置（原生形态）；省略则不改，显式传 null 则清空。"
        ),
    )


async def _ensure_knowledge_bases_visible(
    access: ResourceAccessService,
    user_id: str,
    config: SessionKnowledgeConfig | None,
) -> None:
    """校验 ``config`` 里每个 KB 对调用者可见（不可见 → 404）。

    行为与原生 ``_ensure_knowledge_bases_exist`` 一致。
    """
    if config is None or not config.knowledge_base_ids:
        return
    for kb_id in config.knowledge_base_ids:
        await access.get_resource(user_id, ResourceKind.KNOWLEDGE_BASE, kb_id)


async def _resolve_bound_credential(
    storage: StorageBase,
    access: ResourceAccessService,
    user_id: str,
    credential_id: str,
) -> CredentialRecord | None:
    """解析绑定凭证的原始记录；找不到返回 ``None``。

    先按严格归属查（``storage.get_credential``），miss 再退到
    ``access.resolve_credential``（own / 共享；本仓给该方法打了「全局兜底」
    补丁，因此绑定在智能体上的、属于别人的凭证也能解析到）。
    """
    record = await storage.get_credential(user_id, credential_id)
    if record is not None:
        return record
    try:
        return await access.resolve_credential(user_id, credential_id)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
    return None


async def _build_bound_chat_model_config(
    storage: StorageBase,
    access: ResourceAccessService,
    user_id: str,
    agent_id: str,
    chat_model_config: SessionChatModelInput,
    purpose: str = "creating a session",
) -> ChatModelConfig:
    """按 ``agent_id`` 的绑定凭证，把精简模型配置补成原生形态。

    1. 读 ``agent_credential`` 绑定拿 ``credential_id``（无绑定 → 404）
    2. 解析该凭证（严格归属 → 共享 / 全局兜底，找不到 → 404）
    3. 校验其 ``data["type"]`` 必须是 ``bocom_ellm_credential``（否则 → 400）

    Args:
        storage (`StorageBase`): 注入的存储后端。
        access (`ResourceAccessService`): 注入的资源可见性服务。
        user_id (`str`): 调用者（用于凭证可见性解析）。
        agent_id (`str`): 提供绑定的智能体 id。
        chat_model_config (`SessionChatModelInput`): 请求体里的精简配置。
        purpose (`str`): 仅用于错误文案（"creating a session" /
            "updating a session"）。

    Returns:
        `ChatModelConfig`: ``type`` 固定 ``bocom_ellm_credential``、
        ``credential_id`` 取自绑定，``model`` / ``parameters`` 取自入参。

    Raises:
        `HTTPException`: 404 无绑定 / 凭证不可解析；400 绑定凭证类型不符。
    """
    credential_id = await get_agent_credential_id(agent_id)
    if not credential_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Agent {agent_id!r} has no binding in "
                f"'agent_credential'; bind a credential before {purpose}."
            ),
        )

    record = await _resolve_bound_credential(
        storage,
        access,
        user_id,
        credential_id,
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Credential {credential_id!r} bound to agent "
                f"{agent_id!r} is not found or not resolvable."
            ),
        )
    bound_type = (record.data or {}).get("type")
    if bound_type != ELLM_CREDENTIAL_TYPE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Credential {credential_id!r} is type {bound_type!r}, "
                f"not {ELLM_CREDENTIAL_TYPE!r}."
            ),
        )

    return ChatModelConfig(
        type=ELLM_CREDENTIAL_TYPE,
        credential_id=credential_id,
        model=chat_model_config.model,
        parameters=chat_model_config.parameters,
    )


@session_usage_router.post(
    "/create",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建会话并自动绑定该智能体的 ELLM 凭证",
    responses={
        400: {
            "description": "绑定凭证类型不是 'bocom_ellm_credential'。",
        },
        404: {
            "description": (
                "智能体不可见 / 该智能体未绑定凭证 / 凭证不可解析 / "
                "知识库不可见。"
            ),
        },
    },
)
async def create_session_with_credential(
    body: CreateSessionWithCredentialRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> CreateSessionResponse:
    """创建会话：``chat_model_config`` 的 ``type`` / ``credential_id`` 自动注入。

    除凭证注入外，语义与原生 ``POST /api/sessions`` 保持一致：

    1. 校验 ``agent_id`` 对调用者可见（own / shared，否则 404）
    2. 组装原生形态 ``ChatModelConfig``：读 ``agent_credential`` 绑定得到
       ``credential_id``（无绑定 → 404），解析该凭证（严格归属 → 共享/全局
       兜底，找不到 → 404），校验 ``data["type"]`` 必须是
       ``bocom_ellm_credential``（否则 → 400）——见
       :func:`_build_bound_chat_model_config`
    3. 校验知识库可见性；解析 ``workspace_id``（显式传入优先）
    4. ``storage.upsert_session(...)`` 落库 —— 结构与原生接口完全一致
       （同一 ``(user_id, agent_id, workspace_id)`` 三元组为 upsert）

    Args:
        body (`CreateSessionWithCredentialRequest`): 请求体，见该模型。
        user_id (`str`): 注入的 ``X-User-ID``。
        storage (`StorageBase`): 注入的存储后端。
        workspace_manager (`WorkspaceManagerBase`): 用于分配 workspace。
        access (`ResourceAccessService`): 注入的资源可见性服务。

    Returns:
        `CreateSessionResponse`: ``{"session_id": "..."}``（与原生一致）。

    Raises:
        `HTTPException`:
            - 404：agent 不可见 / 未绑定凭证 / 凭证不可解析 / KB 不可见
            - 400：绑定凭证类型不是 ``bocom_ellm_credential``
    """
    # 1) agent 可见性 —— 与原生 create_session 首步一致
    await access.resolve_agent(user_id, body.agent_id)

    # 2) 绑定凭证：读 agent_credential（无绑定 → 404），解析（不可解析 →
    #    404），校验类型（非 ELLM → 400），补齐 type / credential_id
    chat_model_config = await _build_bound_chat_model_config(
        storage,
        access,
        user_id,
        body.agent_id,
        body.chat_model_config,
    )
    credential_id = chat_model_config.credential_id

    # 3) 知识库可见性（与原生一致）+ workspace 解析（显式传入优先）
    await _ensure_knowledge_bases_visible(
        access,
        user_id,
        body.knowledge_config,
    )
    resolved_workspace_id = body.workspace_id or (
        workspace_manager.assign_workspace_id(
            user_id=user_id,
            agent_id=body.agent_id,
            session_id=_generate_id(),
        )
    )

    # 4) 落库：与原生 create_session 相同的调用与字段
    session_record = await storage.upsert_session(
        user_id=user_id,
        agent_id=body.agent_id,
        config=SessionConfig(
            workspace_id=resolved_workspace_id,
            chat_model_config=chat_model_config,
            fallback_chat_model_config=body.fallback_chat_model_config,
            tts_model_config=body.tts_model_config,
            knowledge_config=body.knowledge_config,
            **({"name": body.name} if body.name is not None else {}),
        ),
    )
    logger.info(
        "session create with credential: user=%s agent=%s credential=%s "
        "session=%s",
        user_id,
        body.agent_id,
        credential_id,
        session_record.id,
    )
    return CreateSessionResponse(session_id=session_record.id)


# ---------------------------------------------------------------------------
# 更新会话：同样自动注入该智能体绑定的 ELLM 凭证
# ---------------------------------------------------------------------------


@session_usage_router.post(
    "/update",
    response_model=SessionRecord,
    summary="更新会话并自动绑定该智能体的 ELLM 凭证",
    responses={
        400: {
            "description": "绑定凭证类型不是 'bocom_ellm_credential'。",
        },
        404: {
            "description": (
                "会话不存在 / 该智能体未绑定凭证 / 凭证不可解析 / "
                "知识库不可见。"
            ),
        },
    },
)
async def update_session_with_credential(
    body: UpdateSessionWithCredentialRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> SessionRecord:
    """更新会话：``chat_model_config`` 的 ``type`` / ``credential_id`` 自动注入。

    除凭证注入外，语义与原生 ``PATCH /api/sessions/{session_id}`` 保持一致：

    1. 校验会话属于调用者（``user_id`` + ``agent_id`` + ``session_id``，
       否则 404）
    2. 请求体给了 ``chat_model_config``（非 null）时，按 ``agent_id`` 的
       ``agent_credential`` 绑定补齐 ``type`` / ``credential_id``（无绑定 /
       凭证不可解析 → 404；类型不符 → 400）——见
       :func:`_build_bound_chat_model_config`
    3. 校验知识库可见性
    4. PATCH 语义落库：只覆盖请求体里**显式出现**的字段
       （``exclude_unset=True``；显式传 ``null`` = 清空），
       ``storage.upsert_session(..., session_id=...)`` 与原生 PATCH 一致

    说明：原生 ``UpdateSessionRequest.permission_mode`` 未在此暴露——该字段
    改的是会话权限状态、与凭证注入无关，需要时走原生 PATCH。

    Args:
        body (`UpdateSessionWithCredentialRequest`): 请求体，见该模型。
        user_id (`str`): 注入的 ``X-User-ID``。
        storage (`StorageBase`): 注入的存储后端。
        access (`ResourceAccessService`): 注入的资源可见性服务。

    Returns:
        `SessionRecord`: 更新后的完整会话记录（与原生 PATCH 一致）。

    Raises:
        `HTTPException`:
            - 404：会话不存在 / 未绑定凭证 / 凭证不可解析 / KB 不可见
            - 400：绑定凭证类型不是 ``bocom_ellm_credential``
    """
    # 1) 会话必须属于调用者（与原生 PATCH 首步一致）
    existing = await storage.get_session(
        user_id,
        body.agent_id,
        body.session_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{body.session_id}' not found.",
        )

    # 2) 给了模型配置（非 null）→ 用该智能体的绑定凭证补齐
    injected_chat_model_config: ChatModelConfig | None = None
    if body.chat_model_config is not None:
        injected_chat_model_config = await _build_bound_chat_model_config(
            storage,
            access,
            user_id,
            body.agent_id,
            body.chat_model_config,
            purpose="updating a session",
        )

    # 3) 知识库可见性（与原生一致）
    await _ensure_knowledge_bases_visible(
        access,
        user_id,
        body.knowledge_config,
    )

    # 4) PATCH 语义：exclude_unset 区分「省略（不改）」与「显式 null（清空）」
    config_updates: dict[str, Any] = body.model_dump(
        exclude_unset=True,
        exclude={"session_id", "agent_id", "chat_model_config"},
    )
    if "chat_model_config" in body.model_fields_set:
        config_updates["chat_model_config"] = (
            None
            if injected_chat_model_config is None
            else injected_chat_model_config.model_dump(mode="json")
        )

    record = await storage.upsert_session(
        user_id=user_id,
        agent_id=body.agent_id,
        config=SessionConfig.model_validate(
            {**existing.config.model_dump(mode="json"), **config_updates},
        ),
        state=existing.state,
        session_id=body.session_id,
    )
    logger.info(
        "session update with credential: user=%s agent=%s session=%s "
        "fields=%s",
        user_id,
        body.agent_id,
        body.session_id,
        sorted(body.model_fields_set),
    )
    return record
