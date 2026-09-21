# -*- coding: utf-8 -*-
"""智能体模板名单表（``agent_template``）+ 异步访问层。

与 ``market_store.py`` / ``team_store.py`` 同一套模式：bocomadp 自建
declarative base，复用框架存储的 ``_engine`` / ``_session_factory``，
启动时 :func:`ensure_agent_template_tables` 幂等建表（**自动创建，无需
手工 DDL / 迁移**），``src/`` 框架零改动。

表语义（**白名单表**，一行 = 一个可被复制的模板智能体）::

    agent_id      主键，即 ``agents.id``（1:1，删智能体应级联删行）
    owner_user_id 该智能体的归属用户（``agents.user_id``）——用于
                  owner-scoped 读取源记录（``storage.get_agent`` 按 owner
                  定位，模板通常归属 ``default``）
    title         模板展示名（空串 = 用智能体本名）
    description   模板说明（一句话价值点）
    category      分类/分组（前端按类目分组展示）
    sort_order    展示顺序（小者在前；同值用 created_at 兜底）
    enabled       是否可复制（临时下架而不删行）
    created_at / updated_at  审计时间戳

与 ``agent_market`` 的关系：互不影响。市场表管"是否在应用市场上架"，
本表管"是否允许被复制"；同一智能体可以同时在两表里。

列类型约定（与 ``market_store`` 一致）：需要索引/默认值的列一律
``VARCHAR``（MySQL 不支持给 TEXT 设默认值、也不能直接索引）；时间戳由
应用侧赋值（规避 MySQL 首个 TIMESTAMP 隐式 ``ON UPDATE`` 的坑）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("bocomadp.agent_template_store")

# ---------------------------------------------------------------------------
# SQLAlchemy 表（独立 metadata，启动时与框架表一起建）
# ---------------------------------------------------------------------------


class _TemplateBase(DeclarativeBase):
    """bocomadp 专用 declarative base：只服务 agent_template 表。"""


class AgentTemplateRow(_TemplateBase):
    """``agent_template`` 表：一行 = 一个可复制模板智能体。"""

    __tablename__ = "agent_template"

    agent_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    owner_user_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
    )
    description: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="",
    )
    category: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# 业务模型
# ---------------------------------------------------------------------------


class AgentTemplateEntry(BaseModel):
    """一条模板名单记录（对应表里的一行）。"""

    agent_id: str
    owner_user_id: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    sort_order: int = 0
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


#: 允许通过 :func:`update_template_entry` 部分更新的列白名单。
_UPDATABLE_FIELDS: tuple[str, ...] = (
    "owner_user_id",
    "title",
    "description",
    "category",
    "sort_order",
    "enabled",
)


# ---------------------------------------------------------------------------
# 异步访问层（storage 参数就是框架 storage，含 _engine/_session_factory）
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """DateTime() 无时区列统一用 UTC naive 时间（与 team_store 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_factory(storage: Any) -> Any:
    return getattr(storage, "_session_factory", None)


def _to_entry(row: AgentTemplateRow) -> AgentTemplateEntry:
    """ORM 行 → 业务模型。"""
    return AgentTemplateEntry(
        agent_id=row.agent_id,
        owner_user_id=row.owner_user_id,
        title=row.title,
        description=row.description,
        category=row.category,
        sort_order=row.sort_order,
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def ensure_agent_template_tables(storage: Any) -> None:
    """启动时建表（幂等，已存在则跳过）+ 孤儿名单清理。

    新表直接 ``create_all``，**不需要迁移脚本**；本地旧库重复启动无副作用。
    Redis 等无 ``_engine`` 的存储只打 warning 后跳过（副本功能不生效）。
    """
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning(
            "storage has no _engine yet; skip agent_template provisioning",
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_TemplateBase.metadata.create_all)
    logger.info("ensured table agent_template")

    pruned = await prune_orphan_template_entries(storage)
    if pruned:
        logger.info("pruned %d orphan agent_template entries", pruned)


async def create_template_entry(
    storage: Any,
    entry: AgentTemplateEntry,
) -> bool:
    """把一个智能体加入模板名单。

    不存在则插行并返回 ``True``；``agent_id`` 已在名单内返回 ``False``
    （**不覆盖**已有配置——改配置走 :func:`update_template_entry`）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return False
    now = _now()
    async with factory() as session:
        if await session.get(AgentTemplateRow, entry.agent_id) is not None:
            return False
        session.add(
            AgentTemplateRow(
                agent_id=entry.agent_id,
                owner_user_id=entry.owner_user_id,
                title=entry.title,
                description=entry.description,
                category=entry.category,
                sort_order=entry.sort_order,
                enabled=entry.enabled,
                created_at=now,
                updated_at=now,
            ),
        )
        await session.commit()
        return True


async def get_template_entry(
    storage: Any,
    agent_id: str,
) -> AgentTemplateEntry | None:
    """按 agent_id 取一条模板记录；不在名单内返回 ``None``。"""
    factory = _session_factory(storage)
    if factory is None:
        return None
    async with factory() as session:
        row = await session.get(AgentTemplateRow, agent_id)
        return None if row is None else _to_entry(row)


async def is_copyable_template(
    storage: Any,
    agent_id: str,
) -> AgentTemplateEntry | None:
    """复制端点专用：在名单内**且** ``enabled`` 才返回条目，否则 ``None``。"""
    entry = await get_template_entry(storage, agent_id)
    if entry is None or not entry.enabled:
        return None
    return entry


async def list_template_entries(
    storage: Any,
    *,
    category: str | None = None,
    enabled: bool | None = None,
) -> list[AgentTemplateEntry]:
    """列出模板名单。

    固定按 ``sort_order ASC, created_at DESC, agent_id ASC`` 排序——
    末尾的 agent_id 是必要的 tie-breaker，否则同 ``sort_order`` 行的返回
    顺序不稳定，前端分页会重复/漏项。

    Args:
        storage (`Any`):
            框架 storage（需含 ``_session_factory``）。
        category (`str | None`, optional):
            可选分类精确过滤（``None`` = 不过滤）。
        enabled (`bool | None`, optional):
            可选启停过滤（``None`` = 全部，含已下架）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return []
    stmt = select(AgentTemplateRow)
    if category is not None:
        stmt = stmt.where(AgentTemplateRow.category == category)
    if enabled is not None:
        stmt = stmt.where(AgentTemplateRow.enabled.is_(enabled))
    stmt = stmt.order_by(
        AgentTemplateRow.sort_order.asc(),
        AgentTemplateRow.created_at.desc(),
        AgentTemplateRow.agent_id.asc(),
    )
    async with factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [_to_entry(r) for r in rows]


async def update_template_entry(
    storage: Any,
    agent_id: str,
    patch: dict[str, Any],
) -> AgentTemplateEntry | None:
    """部分更新模板记录（PATCH 语义：只改 ``patch`` 里出现的列）。

    未在名单内返回 ``None``（调用方映射 404）；``patch`` 中非白名单键、
    ``None`` 值会被忽略（避免把列写成 NULL）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return None
    async with factory() as session:
        row = await session.get(AgentTemplateRow, agent_id)
        if row is None:
            return None
        for key, value in patch.items():
            if key in _UPDATABLE_FIELDS and value is not None:
                setattr(row, key, value)
        row.updated_at = _now()
        await session.commit()
        return _to_entry(row)


async def delete_template_entry(storage: Any, agent_id: str) -> bool:
    """把智能体移出模板名单；删到返回 ``True``，本来就不在名单内返回 ``False``。"""
    factory = _session_factory(storage)
    if factory is None:
        return False
    async with factory() as session:
        row = await session.get(AgentTemplateRow, agent_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def prune_orphan_template_entries(storage: Any) -> int:
    """清理孤儿模板行（``agents`` 表里已不存在的智能体）。

    删除智能体时路由层会级联删行，但团队成员级联删除 / 手工删库等路径
    可能绕过，故启动时兜底扫一遍，保证名单里永远是活智能体。
    """
    factory = _session_factory(storage)
    if factory is None:
        return 0
    try:
        from agentscope.app.storage._sql._tables import AgentRow
    except Exception:  # noqa: BLE001 - 非 SQL 存储（Redis）没有该表
        return 0
    async with factory() as session:
        alive = {
            agent_id
            for (agent_id,) in (
                await session.execute(select(AgentRow.id))
            ).all()
        }
        rows = (
            await session.execute(select(AgentTemplateRow))
        ).scalars().all()
        orphans = [r for r in rows if r.agent_id not in alive]
        for row in orphans:
            await session.delete(row)
        if orphans:
            await session.commit()
        return len(orphans)


__all__ = [
    "AgentTemplateEntry",
    "AgentTemplateRow",
    "create_template_entry",
    "delete_template_entry",
    "ensure_agent_template_tables",
    "get_template_entry",
    "is_copyable_template",
    "list_template_entries",
    "prune_orphan_template_entries",
    "update_template_entry",
]
