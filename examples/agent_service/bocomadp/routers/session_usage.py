# -*- coding: utf-8 -*-
"""会话相关扩展接口（token 用量查询 + 创建会话并自动绑定凭证）。

Endpoint
--------
``GET  /sessions/{session_id}/usage?agent_id=xxx&user_id=xxx``
``GET  /sessions/limit``
``GET  /sessions/usage/agents?user_id=xxx``
``GET  /sessions/usage/history?user_id=xxx&agent_id=xxx``
``POST /sessions/create``
``POST /sessions/update``

    - usage：返回 ``input_tokens`` / ``output_tokens`` / ``message_count``
      （聚合会话内全部已落库消息）。
    - limit：分页返回某智能体的会话记录（直连 DB，COUNT + LIMIT）。
      会话名按下述规则改写后返回（不落库）。
    - usage/agents：某用户**使用过的智能体清单**（sessions 按 agent_id
      分组聚合，含自建与市场智能体，排除系统内置），附会话数、最近
      使用时间、智能体名与市场/自建标记（``is_platform`` = 在
      ``agent_market`` 名单内）。
    - usage/history：某用户**跨智能体的会话历史**（自建 + 平台都在
      内，按 updated_at 倒序统一分页），每条附 agent_name；可选
      agent_id 收窄到单个智能体。会话名改写规则与 limit 相同。
      两个接口的 user_id 均可省略（回退 X-User-ID）。
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
import re
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


# ---------------------------------------------------------------------------
# 会话名改写
# ---------------------------------------------------------------------------
# 建会话时用户还没说话，框架因此用创建时间兜底当会话名（见
# ``SessionConfig.name`` 的 default_factory）。前端侧栏于是一片日期。
# 这里在 ``GET /sessions/limit`` 返回前把这类"默认时间名"换成更有意义的
# 显示名：有用户输入就用首句话，没有就显示"新对话"。用户改过名的一律不动。
#
# 说明两点：
#   1. 只改响应不落库（写的收益小于覆盖会话状态的风险）；
#   2. 判定"是否默认名"靠下面的正则，属启发式——用户若把会话名手动起成
#      "2026-09-14 15:13:26" 也会被改写，概率极低，接受。

#: 框架默认会话名形态（``"%Y-%m-%d %H:%M:%S"``），命中即视为未改名。
_DEFAULT_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

#: 会话名最大长度，超出截断并加省略号。
_TITLE_MAX_LEN = 30

#: 无用户输入时的会话名。
_FALLBACK_SESSION_NAME = "新对话"


def _extract_message_text(content: Any) -> str:
    """从 ``Msg.content`` 里提取纯文本。

    ``content`` 既可能是字符串，也可能是多模态块列表（形如
    ``[{"type": "text", "text": "..."}, ...]``）。其余形态（图片块等）
    一律跳过，返回拼接后的文本（可能为空串）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _derive_session_title(payloads: list[Any]) -> str | None:
    """从按时间升序的消息 payload 列表里取**首条用户输入**做会话名。

    ``payload`` 是完整 ``Msg`` JSON（含 ``role`` / ``content``）。取不到
    有效用户输入时返回 ``None``，由调用方退到 ``_FALLBACK_SESSION_NAME``。
    """
    for raw in payloads:
        msg = raw if isinstance(raw, dict) else _as_payload_dict(raw)
        if not msg or msg.get("role") != "user":
            continue
        text = _extract_message_text(msg.get("content")).strip()
        if not text:
            continue
        # 折叠连续空白为单个空格，避免多行输入把侧栏名字撑爆
        text = re.sub(r"\s+", " ", text)
        if len(text) > _TITLE_MAX_LEN:
            text = text[:_TITLE_MAX_LEN] + "…"
        return text
    return None


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


def _context_usage_of(msg: Any) -> tuple[int, int]:
    """取一条消息的上下文窗口占用 ``(input_tokens, output_tokens)``。

    值来自 ``msg.metadata["context_usage"]``——由 ``main.py`` 的 storage
    proxy 在落库时写入（见 ``_BuiltinAgentStorageProxy.upsert_message``），
    等于该 reply **最后一次**模型调用的 prompt/output 长度。

    **不回退**：没有该字段的消息（本改造之前落库的历史数据）返回
    ``(0, 0)``。框架的 ``msg.usage`` 是累加口径（一个 reply 内多次调用
    之和，多轮工具循环后偏大 N 倍），与"上下文窗口占用"不是同一个量，
    因此不参与取值。

    Args:
        msg (`Any`): 框架 ``Msg`` 实例。

    Returns:
        `tuple[int, int]`:
            ``(input_tokens, output_tokens)``；缺字段 / 非 dict / 值非法时
            为 ``(0, 0)``。
    """
    metadata = getattr(msg, "metadata", None)
    context_usage = (
        metadata.get("context_usage") if isinstance(metadata, dict) else None
    )
    if not isinstance(context_usage, dict):
        return (0, 0)
    return (
        int(context_usage.get("input_tokens") or 0),
        int(context_usage.get("output_tokens") or 0),
    )


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
    """取会话当前的上下文窗口占用（input / output / total tokens）。

    从最新一条消息往前回溯（页内按时间正序返回，因此倒序扫描），取第一条
    带 ``metadata.context_usage`` 的消息即停——该字段由 storage proxy 在
    落库时写入，值 = 该 reply 最后一次模型调用的 prompt/output 长度，即
    当时上下文窗口的真实占用。

    本改造之前落库的历史消息没有该字段，**不回退**到框架的 ``usage``
    （那是"一个 reply 内多次调用之和"，与窗口占用不是同一个量，多轮工具
    循环后偏大 N 倍），此类会话返回 0。

    注意这也不是"整个会话的累计计费量"：一次对话内每轮请求都会重新带上
    全部上下文，把各轮相加会得到远大于窗口长度的数字（实测单条最高放大
    13.8 倍），因此本接口只回答"当前上下文占了多少"。
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

    # 从最新一条消息往前回溯，取最近一条带"上下文窗口占用"的消息（页内按
    # 时间正序返回，因此倒序扫描）；命中即停，找不到才继续翻更早的页。
    # 历史消息（无 context_usage）不参与取值，全都没有时返回 0。
    input_tokens = 0
    output_tokens = 0
    before: str | None = None
    batch_limit = 20

    while True:
        messages, has_more = await storage.list_messages(
            user_id,
            session_id,
            limit=batch_limit,
            before=before,
        )
        if not messages:
            break

        for msg in reversed(messages):
            found_input, found_output = _context_usage_of(msg)
            if found_input > 0:
                input_tokens = found_input
                output_tokens = found_output
                break

        if input_tokens > 0 or not has_more:
            break
        # Move cursor to continue pagination
        before = messages[0].id

    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "message_count": 0,
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

    会话名改写（见模块顶部说明）：名字仍是默认时间名的会话，返回时
    换成本会话首条用户输入；无用户输入的显示 ``"新对话"``。改过名的
    会话不动，且只改响应不落库。
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

    # Current page of session records, newest-first.
    # 两步查（先 id 排序分页，再按 id 回查完整行）：payload 是几十 KB 的
    # 大 JSON，带着它排序会撑爆 MySQL sort buffer（错误 1038
    # Out of sort memory，线上已触发），详见 _paged_session_rows。
    rows = await _paged_session_rows(
        engine,
        where="WHERE user_id = :user_id AND agent_id = :agent_id",
        params={"user_id": user_id, "agent_id": agent_id},
        order_by="created_at DESC, id DESC",
        limit=page_size,
        offset=offset,
    )

    # 行 → SessionRecord dict + 默认时间名改写（公共实现见下方两个
    # helper，与 /sessions/usage/history 共用）。
    sessions = _session_rows_to_records(rows)
    await _rewrite_default_session_names(engine, sessions)

    return {
        "sessions": sessions,
        "total": total,
        "agent_id": agent_id,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(sessions) < total,
    }


# ---------------------------------------------------------------------------
# 公共 helper：裸 SQL 行重建 / 会话名改写 / 智能体简报
# （/sessions/limit 与 /sessions/usage/* 共用）
# ---------------------------------------------------------------------------


def _strip_session_context(session: dict) -> dict:
    """列表接口**不下发会话里的消息明细**（``state.context``）。

    ``sessions`` 与 ``messages`` 是两张表、两种职责：列表接口只给"目录"
    （id / 名字 / 时间），完整对话走 ``GET /sessions/{session_id}/messages``
    分页取。``state.context`` 里塞着全部消息、工具结果、token 用量，会随
    对话轮数**线性膨胀**——数据一多就拖慢响应、甚至超时，所以在列表接口
    统一裁掉。只删 ``context``，``state`` 其余字段（summary / reply_context
    等）保留，兼容需要读会话状态的调用方。
    """
    state = session.get("state")
    if isinstance(state, dict):
        state.pop("context", None)
    return session


async def _paged_session_rows(
    engine: Any,
    where: str,
    params: dict[str, Any],
    order_by: str,
    limit: int,
    offset: int,
) -> list[Any]:
    """分页查会话：**先只查 id 排序分页，再按 id 回查完整行**。

    直接 ``SELECT ..., payload ... ORDER BY ...`` 时，MySQL 要把含巨大
    JSON（``state`` / 消息明细，单条可达几十 KB）的整行塞进 sort buffer
    排序——会话一多就撑爆，报 **1038 Out of sort memory**
    （线上已触发：user_id='admin' 的 /sessions/limit 请求）。
    改成两步：

    1. 只取 ``id``（行宽几十字节，排序几乎不占内存），LIMIT/OFFSET
       仍由 DB 完成，分页语义不变；
    2. 用这十来个 id 回查完整行——**不带 ORDER BY**（否则又要把
       payload 装进排序堆），顺序在 Python 侧按第 1 步的 id 序列还原。

    这样数据库全程不排序大字段，1038 再无触发点；``order_by`` 只作用于
    第 1 步，最终顺序与原写法一致，调用方无感。
    """
    from sqlalchemy import text

    async with engine.connect() as conn:
        id_rows = (
            await conn.execute(
                text(
                    "SELECT id FROM sessions "
                    f"{where} ORDER BY {order_by} "
                    "LIMIT :limit OFFSET :offset",
                ),
                {**params, "limit": limit, "offset": offset},
            )
        ).all()
    if not id_rows:
        return []
    ids = [r.id for r in id_rows]
    placeholders = ", ".join(f":i{i}" for i in range(len(ids)))
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, created_at, updated_at, user_id, agent_id, "
                    "source, source_schedule_id, team_id, payload "
                    f"FROM sessions WHERE id IN ({placeholders})",
                ),
                {f"i{i}": sid for i, sid in enumerate(ids)},
            )
        ).all()
    # 按第 1 步的顺序还原（DB 侧不再排序，避免触碰 payload 排序堆）
    by_id = {r.id: r for r in rows}
    return [by_id[sid] for sid in ids if sid in by_id]


def _session_rows_to_records(rows: list[Any]) -> list[dict]:
    """``text()`` 裸 SQL 查出的 sessions 行 → SessionRecord dict 列表。

    与 SQL 存储 mapper 同样的重建方式：把提升列合并回 ``payload`` 后
    ``model_validate``。注意：``text()`` 裸 SQL 绕过 SQLAlchemy 的 JSON
    结果处理器，``payload``（MySQL/OceanBase 的 JSON 列）会以**字符串**
    回到这里（Postgres/asyncpg 则由驱动自动解码）。必须先解码成 dict，
    否则 ``dict("...")`` 会抛 ``ValueError: dictionary update sequence
    element #0 has length 1``。

    重建后统一走 :func:`_strip_session_context` 裁掉消息明细（列表接口
    不下发对话内容，见其文档）。
    """
    from agentscope.app.storage import SessionRecord

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
            _strip_session_context(
                SessionRecord.model_validate(obj).model_dump(mode="json"),
            ),
        )
    return sessions


async def _rewrite_default_session_names(
    engine: Any,
    sessions: list[dict],
) -> None:
    """默认时间名的会话，用首条用户输入生成显示名（就地改写，不落库）。

    只改响应不落库（写会话状态的风险大于显示收益）；用户手动改过名
    的会话（不命中 ``_DEFAULT_NAME_RE``）一律不动。
    """
    from sqlalchemy import text as _text

    async with engine.connect() as conn:
        for sess in sessions:
            name = (sess.get("config") or {}).get("name") or ""
            if not _DEFAULT_NAME_RE.match(name):
                continue
            payloads = (
                await conn.execute(
                    _text(
                        "SELECT payload FROM messages "
                        "WHERE session_id = :sid "
                        "ORDER BY created_at ASC, msg_id ASC LIMIT 20",
                    ),
                    {"sid": sess["id"]},
                )
            ).scalars().all()
            # 有首条用户输入 → 用它当名字；否则退到"新对话"
            sess["config"]["name"] = (
                _derive_session_title(payloads) or _FALLBACK_SESSION_NAME
            )


async def _agent_brief_map(
    engine: Any,
    agent_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """批量取智能体简报 ``{agent_id: {"name", "owner_user_id"}}``。

    名称藏在 ``payload["data"]["name"]``（框架 mapper 的存储契约）；
    智能体已被删的 id 不在返回映射里，由调用方兜底空值。
    """
    if not agent_ids:
        return {}
    from sqlalchemy import text

    placeholders = ", ".join(f":a{i}" for i in range(len(agent_ids)))
    params = {f"a{i}": aid for i, aid in enumerate(agent_ids)}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, user_id, payload FROM agents "
                    f"WHERE id IN ({placeholders})"
                ),
                params,
            )
        ).all()
    briefs: dict[str, dict[str, str | None]] = {}
    for row in rows:
        raw = row.payload
        obj: dict = (
            json.loads(raw)
            if isinstance(raw, str) and raw
            else dict(raw or {})
        )
        data = obj.get("data")
        name = str(data.get("name", "") or "") if isinstance(data, dict) else ""
        briefs[row.id] = {"name": name, "owner_user_id": row.user_id}
    return briefs


def _resolve_target_user(
    user_id: str | None,
    viewer_id: str,
) -> str:
    """usage 接口的目标用户：显式传参优先，否则回退 X-User-ID。"""
    return (user_id or viewer_id).strip() or viewer_id


# ---------------------------------------------------------------------------
# 用户使用视角：用过哪些智能体 + 跨智能体历史会话
# （前端传 user_id 查任意用户的记录；内部系统暂不做查询权限限制，
#  将来加权限只需在 _resolve_target_user 一处收口）
# ---------------------------------------------------------------------------


@session_usage_router.get(
    "/usage/agents",
    summary="List agents a user has chatted with (grouped from sessions)",
)
async def list_user_used_agents(
    user_id: str | None = Query(
        default=None,
        description="目标用户；省略时回退 X-User-ID。",
    ),
    page: int = Query(default=1, ge=1, description="Page number, starts at 1"),
    size: int = Query(
        default=20,
        ge=1,
        le=200,
        description="Number of items per page (1-200)",
    ),
    viewer_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> dict:
    """某用户**使用过的智能体清单**（含自建与市场智能体），分页返回。

    - 数据源：``sessions`` 表按 ``agent_id`` 分组聚合（会话数 +
      最近使用时间）——聊过就会留下会话，天然涵盖自建与市场智能体；
    - 分页：``page`` / ``size``（url 参数）；``total`` 为**分页前**
      的总条数，``has_more`` 标识是否还有下一页——聚合本身在 SQL
      侧完成（量级 = 用过的智能体数），Python 侧只做切片；
    - 附带智能体名（agents 表 ``payload["data"]["name"]``）、归属者
      ``owner_user_id`` 及 ``is_platform`` / ``is_self`` 标记，前端
      可直接分组渲染。``is_platform`` = 该智能体在市场名单内
      （``agent_market`` 表有行，即市场智能体——``user_id`` 已不再
      承载"平台"语义）；``is_self`` = 归属者就是查询目标用户本人；
    - 排除系统内置智能体（``_`` 开头 id）——内部工具载体，不算
      "用户使用的智能体"；
    - 按最近使用时间倒序。
    """
    from sqlalchemy import text

    from bocomadp.market_store import list_market_entries
    from bocomadp.pool_config import _get_engine

    target = _resolve_target_user(user_id, viewer_id)
    engine = await _get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT agent_id, COUNT(*) AS session_count, "
                    "MAX(updated_at) AS last_used_at "
                    "FROM sessions "
                    "WHERE user_id = :user_id "
                    "GROUP BY agent_id "
                    "ORDER BY last_used_at DESC, agent_id",
                ),
                {"user_id": target},
            )
        ).all()

    usage = [r for r in rows if not r.agent_id.startswith("_")]
    total = len(usage)
    start = (page - 1) * size
    page_rows = usage[start : start + size]

    briefs = await _agent_brief_map(engine, [r.agent_id for r in page_rows])
    market_ids = {e.agent_id for e in await list_market_entries(storage)}
    agents = [
        {
            "agent_id": r.agent_id,
            "name": (briefs.get(r.agent_id) or {}).get("name") or "",
            "owner_user_id": (briefs.get(r.agent_id) or {}).get(
                "owner_user_id",
            ),
            "is_platform": r.agent_id in market_ids,
            "is_self": (
                (briefs.get(r.agent_id) or {}).get("owner_user_id") == target
            ),
            "session_count": int(r.session_count),
            "last_used_at": r.last_used_at,
        }
        for r in page_rows
    ]
    return {
        "user_id": target,
        "agents": agents,
        "total": total,
        "page": page,
        "size": size,
        "has_more": start + len(agents) < total,
    }


@session_usage_router.get(
    "/usage/history",
    summary="Paginated cross-agent session history for a user (direct DB query)",
)
async def list_user_session_history(
    user_id: str | None = Query(
        default=None,
        description="目标用户；省略时回退 X-User-ID。",
    ),
    agent_id: str | None = Query(
        default=None,
        description="可选，收窄到单个智能体；省略 = 该用户的全部会话。",
    ),
    page: int = Query(default=1, ge=1, description="Page number, starts at 1"),
    page_size: int = Query(
        default=20,
        ge=1,
        le=200,
        description="Number of items per page (1-200)",
    ),
    viewer_id: str = Depends(get_current_user_id),
) -> dict:
    """某用户**跨智能体的会话历史**（自建 + 市场/平台智能体都在内）。

    - ``sessions.user_id`` 记的是使用者，所以 ``WHERE user_id = :user``
      天然涵盖该用户与任何智能体（自建、平台、他人发布）的对话——
      与框架 ``GET /sessions/?agent_id=`` 不同，本接口不做智能体
      归属校验（框架那条对平台智能体会 404）；
    - 按 ``updated_at`` 倒序统一分页（COUNT + LIMIT/OFFSET 推到 DB）；
    - 每条附 ``agent_name``（批量查 agents 表，删掉的智能体为空串）；
    - 会话名改写规则与 ``/sessions/limit`` 相同（默认时间名 → 首条
      用户输入，只改响应不落库）；
    - 可选 ``agent_id`` 收窄到单个智能体（等价于 /limit 但带名称与
      统一分页形态）。
    """
    from sqlalchemy import text

    from bocomadp.pool_config import _get_engine

    target = _resolve_target_user(user_id, viewer_id)
    engine = await _get_engine()

    where = "WHERE user_id = :user_id"
    params: dict[str, Any] = {"user_id": target}
    if agent_id:
        where += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    else:
        # 系统内置智能体（_ 开头）的会话不算"使用记录"（与
        # /usage/agents 口径一致）；显式传 agent_id 则尊重调用方。
        where += " AND substr(agent_id, 1, 1) <> '_'"

    async with engine.connect() as conn:
        total = (
            await conn.execute(
                text(f"SELECT COUNT(*) FROM sessions {where}"),
                params,
            )
        ).scalar_one()

    offset = (page - 1) * page_size
    # 同 /limit：两步查，避免带着 payload 排序撑爆 sort buffer（错误 1038）
    rows = await _paged_session_rows(
        engine,
        where=where,
        params=params,
        order_by="updated_at DESC, created_at DESC, id DESC",
        limit=page_size,
        offset=offset,
    )

    sessions = _session_rows_to_records(rows)
    briefs = await _agent_brief_map(
        engine,
        sorted({s["agent_id"] for s in sessions}),
    )
    for sess in sessions:
        sess["agent_name"] = (briefs.get(sess["agent_id"]) or {}).get(
            "name",
        ) or ""
    await _rewrite_default_session_names(engine, sessions)

    return {
        "sessions": sessions,
        "total": total,
        "user_id": target,
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
