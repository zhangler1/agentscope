# -*- coding: utf-8 -*-
"""智能体模板名单表（``agent_template``）+ 异步访问层。

与 ``market_store.py`` / ``team_store.py`` 同一套模式：bocomadp 自建
declarative base，复用框架存储的 ``_engine`` / ``_session_factory``，
启动时 :func:`ensure_agent_template_tables` 幂等建表（**自动创建，无需
手工 DDL / 迁移**），``src/`` 框架零改动。

表语义（**白名单表**，一行 = 一个可被复制的模板智能体）::

    agent_id      主键，即 ``agents.id``（1:1，删智能体应级联删行）
    owner_user_id 该智能体的归属用户——**由调用方填写，后端不做任何校验**
                  （可任意值 / 空串，也允许填不存在的用户），纯登记信息：
                  明细端点是按 ``agent_id`` 跨 owner 取记录的，不读这一列
    title         模板展示名（空串 = 用智能体本名）
    description   模板说明（一句话价值点）
    category      分类/分组（前端按类目分组展示）
    skills        期望安装的技能清单（JSON 字符串数组，元素为
                  ``namespace:name``，如 ``["global:rollback-check-sql"]``）
    sort_order    展示顺序（小者在前；同值用 created_at 兜底）
    enabled       是否可复制（临时下架而不删行）
    created_at / updated_at  审计时间戳

与 ``agent_market`` 的关系：互不影响。市场表管"是否在应用市场上架"，
本表管"是否允许被复制"；同一智能体可以同时在两表里。

列类型约定（与 ``market_store`` 一致）：需要索引/默认值的列一律
``VARCHAR``（MySQL 不支持给 TEXT 设默认值、也不能直接索引）；时间戳由
应用侧赋值（规避 MySQL 首个 TIMESTAMP 隐式 ``ON UPDATE`` 的坑）。
``skills`` 用 SQLAlchemy ``JSON`` 列（与 ``team_store.members`` 同模式），
落库为 MySQL/PG 原生 JSON 类型。

**老库升级**：``create_all`` 只建缺失的**表**、不会给已存在的表加**列**，
因此 :func:`ensure_agent_template_tables` 启动时会自检 ``skills`` 列并
自动补 DDL（见 :func:`_ensure_columns`）；自动补列失败时只打 warning，
由运维手工执行（DDL 见该函数日志）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    select,
)
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
    skills: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
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
    skills: list[str] = Field(default_factory=list)
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
    "skills",
    "sort_order",
    "enabled",
)


def normalize_skills(values: list[str] | None) -> list[str]:
    """规范化技能清单：``strip`` → 丢空串 → 去重（**保序**）。

    元素约定为 ``namespace:name`` 形式（如 ``global:rollback-check-sql``，
    与 ``skill_router`` 的 full name 口径一致）；**不做存在性校验**（技能在
    外部 hub，可能被卸载/改名），只保证写进库的是干净的字符串数组。

    写入路径（:func:`create_template_entry` / :func:`update_template_entry`）
    统一经过这里，避免脏值（含前后空格、重复项、空串）入库。

    Args:
        values (`list[str] | None`):
            原始清单；``None`` 视为空。

    Returns:
        `list[str]`:
            去重且保序的干净清单。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


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
        skills=list(row.skills or []),
        sort_order=row.sort_order,
        enabled=bool(row.enabled),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: 老库补列 DDL（按方言）：加可空列 → 回填默认值 → 置 NOT NULL。
#: 只覆盖 ``skills``；将来再加列时在这里追加一份即可。
_COLUMN_DDL: dict[str, tuple[str, ...]] = {
    "mysql": (
        "ALTER TABLE agent_template ADD COLUMN skills JSON NULL",
        "UPDATE agent_template SET skills = JSON_ARRAY() WHERE skills IS NULL",
        "ALTER TABLE agent_template MODIFY COLUMN skills JSON NOT NULL",
    ),
    "mariadb": (
        "ALTER TABLE agent_template ADD COLUMN skills JSON NULL",
        "UPDATE agent_template SET skills = JSON_ARRAY() WHERE skills IS NULL",
        "ALTER TABLE agent_template MODIFY COLUMN skills JSON NOT NULL",
    ),
    "postgresql": (
        "ALTER TABLE agent_template ADD COLUMN skills JSONB NULL",
        "UPDATE agent_template SET skills = '[]'::jsonb WHERE skills IS NULL",
        "ALTER TABLE agent_template ALTER COLUMN skills SET NOT NULL",
    ),
}


def _missing_skills_column(sync_conn: Any) -> bool:
    """``agent_template`` 已存在但缺 ``skills`` 列？（表不存在则返回 False）。"""
    from sqlalchemy import inspect as _sa_inspect

    inspector = _sa_inspect(sync_conn)
    if not inspector.has_table("agent_template"):
        return False   # 表不存在 → create_all 会按新结构建好
    return "skills" not in {
        col["name"] for col in inspector.get_columns("agent_template")
    }


async def _ensure_columns(engine: Any) -> None:
    """老库升级：给**已存在**的 ``agent_template`` 表补 ``skills`` 列。

    ``create_all`` 只建缺失的表、不会给已存在的表加列，所以老库必须补 DDL。
    这里在启动时自检一次：缺列就按方言执行 :data:`_COLUMN_DDL`（幂等，
    已存在则跳过）。

    自动补列**失败不影响启动**（只打 warning 并打印需要手工执行的 DDL）：
    生产上若账号无 DDL 权限，运维照日志执行即可。
    """
    try:
        async with engine.connect() as conn:
            need = await conn.run_sync(_missing_skills_column)
    except Exception:  # noqa: BLE001 —— 自检失败不该阻断启动
        logger.warning(
            "agent_template: column self-check failed; "
            "if 'skills' column is missing, run the DDL manually",
            exc_info=True,
        )
        return

    if not need:
        return

    dialect = getattr(getattr(engine, "dialect", None), "name", "")
    statements = _COLUMN_DDL.get(dialect)
    if statements is None:
        logger.warning(
            "agent_template 缺列 'skills'，方言 %r 未内置补列 DDL，"
            "请手工执行：ALTER TABLE agent_template ADD COLUMN skills "
            "JSON NULL; UPDATE ... SET skills = '[]' ...; "
            "ALTER ... SET NOT NULL",
            dialect,
        )
        return

    from sqlalchemy import text

    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
        logger.info(
            "agent_template: auto-added missing column 'skills' (%s)",
            dialect,
        )
    except Exception:  # noqa: BLE001 —— 补列失败只提示，不阻断启动
        logger.warning(
            "agent_template: 自动补列 'skills' 失败，请手工执行以下 DDL：\n%s",
            "\n".join(f"  {s};" for s in statements),
            exc_info=True,
        )


async def ensure_agent_template_tables(storage: Any) -> None:
    """启动时建表（幂等，已存在则跳过）+ 老库补列 + 孤儿名单清理。

    新表直接 ``create_all``，**不需要迁移脚本**；已存在的表由
    :func:`_ensure_columns` 自检补 ``skills`` 列（``create_all`` 不会给
    已存在的表加列）。Redis 等无 ``_engine`` 的存储只打 warning 后跳过
    （副本功能不生效）。
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

    await _ensure_columns(engine)

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
                skills=normalize_skills(entry.skills),
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


async def list_template_categories(
    storage: Any,
    *,
    enabled: bool | None = None,
) -> list[str]:
    """列出模板名单里出现过的分类（去重 + 升序）。

    与 :func:`list_template_entries` 的 ``category`` 过滤是同一列，供前端
    渲染"分类下拉"用。规则：

    - **排除空分类**（``category == ""``，即新增模板时没填分类的行）——
      它不是一个可展示的类目，前端要"不过滤"直接不传 ``category`` 即可；
    - 去重后按字典序升序返回，顺序稳定（不含数据库默认序的不确定性）；
    - ``enabled`` 可选：只统计上架（``true``）/ 已下架（``false``）的模板，
      与列表端点的同名参数口径一致。

    Args:
        storage (`Any`):
            框架 storage（需含 ``_session_factory``）。
        enabled (`bool | None`, optional):
            可选启停过滤（``None`` = 全部，含已下架）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return []
    stmt = select(AgentTemplateRow.category).where(
        AgentTemplateRow.category != "",
    )
    if enabled is not None:
        stmt = stmt.where(AgentTemplateRow.enabled.is_(enabled))
    stmt = stmt.distinct().order_by(AgentTemplateRow.category.asc())
    async with factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [c for c in rows if c]


async def update_template_entry(
    storage: Any,
    agent_id: str,
    patch: dict[str, Any],
) -> AgentTemplateEntry | None:
    """部分更新模板记录（PATCH 语义：只改 ``patch`` 里出现的列）。

    未在名单内返回 ``None``（调用方映射 404）；``patch`` 中非白名单键、
    ``None`` 值会被忽略（避免把列写成 NULL）——所以 ``skills`` 传 ``null``
    是"不改"，要清空请传 ``[]``。``skills`` 会经 :func:`normalize_skills`
    规范化后再落库。
    """
    factory = _session_factory(storage)
    if factory is None:
        return None
    if patch.get("skills") is not None:
        patch = {**patch, "skills": normalize_skills(patch["skills"])}
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
    "list_template_categories",
    "list_template_entries",
    "normalize_skills",
    "prune_orphan_template_entries",
    "update_template_entry",
]
