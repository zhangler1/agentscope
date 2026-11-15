# -*- coding: utf-8 -*-
"""reviewer 管理员跨用户解析真实 owner 的共用工具（业务路由层，不碰 src/）。

与 ``routers/agent.py`` 的 ``_reviewer_resolve_agent`` 同款逻辑抽出来给
tools / 知识库（cross-search-config / cross-search-refs）等路由复用：

框架的 ``resolve_for_edit`` / ``storage.get_agent(user_id, ...)`` 都按"调用人"
隔离，reviewer 尽管在列表里看得到全量，碰到别人名下的资源就会 404 / 落到
自己名下。这里按 ``agent_id`` 直查 ``agents`` 表反查真实 ``owner_id``，再以
owner 身份操作——与智能体改 / 删改动同构。
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from sqlalchemy import text


async def reviewer_resolve_owner_id(
    storage: Any,
    user_id: str,
    agent_id: str,
) -> Optional[str]:
    """返回智能体真实 ``owner_id``。

    - 调用人是 reviewer：绕过框架按 viewer 隔离，直查 agents 表拿真实 owner；
      智能体不存在（或 storage 无引擎）返回 ``None``。
    - 否则：调用人即 owner，直接返回 ``user_id``（存在性由后续
      ``storage.get_agent`` 校验）。
    """
    from bocomadp import role_store

    is_reviewer = await role_store.has_role(user_id, role_store.REVIEWER_ROLE)
    if not is_reviewer:
        return user_id

    engine = getattr(storage, "_engine", None)
    if engine is None:
        return None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT user_id FROM agents "
                    "WHERE id = :aid AND source = 'user'",
                ),
                {"aid": agent_id},
            )
        ).first()
    if row is None:
        return None
    return row[0]


async def reviewer_resolve_agent(
    request: Any,
    user_id: str,
    agent_id: str,
) -> Tuple[str, Any] | None:
    """reviewer 也能解析出他人智能体，返回 ``(owner_id, record)``；找不到返回 None。

    取代各路由原 ``_resolve_agent`` / ``_resolve_framework_agent``：普通用户仍按
    自身 ``user_id`` 解析（保留原 404 行为），reviewer 按真实 owner 解析。
    """
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        return None
    owner_id = await reviewer_resolve_owner_id(storage, user_id, agent_id)
    if owner_id is None:
        return None
    try:
        record = await storage.get_agent(owner_id, agent_id)
    except Exception:  # noqa: BLE001
        return None
    if record is None:
        return None
    return owner_id, record
