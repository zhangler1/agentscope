# -*- coding: utf-8 -*-
"""智能体市场扩展表（``agent_market``）+ 异步访问层。

与 ``team_store.py`` 同一套模式：bocomadp 自建 declarative base，复用框架
存储的 ``_engine`` / ``_session_factory``，启动时 ``ensure_market_tables``
幂等建表，``src/`` 框架零改动。

设计约定：

- 领域分类即 ``agent_market.tag`` 自由字符串字段（≤64 字符）：
  打标内容原样存储，无预设清单、无权限体系（初版内部使用）；
- 热度不落库（实时聚合 sessions，见 ``routers/market.py``）。

表语义（一行 = 一个智能体的市场档案，1:1 关联 ``agents.id``）：

- ``agent_id``     主键，即 ``agents.id``；
- ``tag``          自由标签；平台智能体创建时自动写入默认标签
  （config ``agent_market.default_tag``，当前"未分类"），
  清标重置回默认值；
- ``published``    是否发布到市场：平台智能体建档即 ``1``（天然在市场）；
  用户智能体默认 ``0``，由 ``POST /agent/market/{id}/publish`` 置 1、
  ``/unpublish`` 撤回置 0（标签保留不动）；
- ``published_at`` 最近一次发布时间（撤回时清空）；
- ``created_at`` / ``updated_at`` 档案时间戳。

用户智能体的档案行**懒式创建**：发布或打标时才建档——未发布、
未打标的个人智能体在表里没有行；启动兜底只补平台智能体。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, Index, String, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("bocomadp.market_store")

# ---------------------------------------------------------------------------
# SQLAlchemy 表（独立 metadata，启动时与框架表一起建）
# ---------------------------------------------------------------------------


class _MarketBase(DeclarativeBase):
    """bocomadp 专用 declarative base：只服务 agent_market 表。"""


class AgentMarketRow(_MarketBase):
    __tablename__ = "agent_market"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tag: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)

    __table_args__ = (Index("ix_agent_market_tag", "tag"),)


# ---------------------------------------------------------------------------
# 业务模型
# ---------------------------------------------------------------------------


class AgentMarketEntry(BaseModel):
    """一条市场档案（对应表里的一行）。"""

    agent_id: str
    tag: str = ""
    published: bool = False
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# 异步访问层（storage 参数就是框架 storage，含 _engine/_session_factory）
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """DateTime() 无时区列统一用 UTC naive 时间（与 team_store 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_factory(storage: Any) -> Any:
    return getattr(storage, "_session_factory", None)


async def _migrate_domain_to_tag(conn: Any) -> None:
    """旧表结构迁移：加 ``tag`` 列 + 删遗留的 ``domain`` 列。

    - ``create_all`` 不会给已存在的表加列，缺 ``tag`` 就 ``ALTER ADD``；
    - **旧 ``domain`` 列必须删**：它是 ``NOT NULL`` 且 MySQL 端无默认值
      （原默认值是 SQLAlchemy 在 Python 端补的），新代码 INSERT 不再
      提供它，留着会导致每次写档案都报 1364
      ``Field 'domain' doesn't have a default value``；
    - 两步都做幂等判断（列不存在则跳过），SQLite/MySQL 通用。
    """
    from sqlalchemy import inspect as sa_inspect

    def _cols(sync_conn: Any) -> set[str]:
        insp = sa_inspect(sync_conn)
        return {c["name"] for c in insp.get_columns("agent_market")}

    cols = await conn.run_sync(_cols)
    if "tag" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE agent_market "
                "ADD COLUMN tag VARCHAR(64) NOT NULL DEFAULT ''",
            ),
        )
        logger.info("migrated agent_market: added tag column (was domain)")
    if "domain" in cols:
        # MySQL 删列时同名单列索引（ix_agent_market_domain）随之删除
        await conn.execute(text("ALTER TABLE agent_market DROP COLUMN domain"))
        logger.info("migrated agent_market: dropped legacy domain column")


async def _migrate_publish_columns(conn: Any) -> None:
    """旧表结构迁移：补 ``published`` / ``published_at`` 两列（幂等）。

    ``create_all`` 不会给已存在的表加列，缺列就 ``ALTER ADD``。
    TINYINT(1) / DATETIME 在 MySQL 与 SQLite 上通用（BOOLEAN 是
    MySQL 里 TINYINT(1) 的别名，SQLite 只看类型亲和性）。
    存量平台档案的 ``published=1`` 回填由 ``ensure_market_tables``
    里的 ``_backfill_platform_published`` 负责（需要跨表查询）。
    """
    from sqlalchemy import inspect as sa_inspect

    def _cols(sync_conn: Any) -> set[str]:
        insp = sa_inspect(sync_conn)
        return {c["name"] for c in insp.get_columns("agent_market")}

    cols = await conn.run_sync(_cols)
    if "published" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE agent_market "
                "ADD COLUMN published TINYINT(1) NOT NULL DEFAULT 0",
            ),
        )
        logger.info("migrated agent_market: added published column")
    if "published_at" not in cols:
        await conn.execute(
            text(
                "ALTER TABLE agent_market "
                "ADD COLUMN published_at DATETIME NULL",
            ),
        )
        logger.info("migrated agent_market: added published_at column")


async def ensure_market_tables(storage: Any) -> None:
    """启动时建表（幂等，已存在则跳过）+ 迁移 + 孤儿清理 + 平台档案补齐。"""
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning(
            "storage has no _engine yet; skip agent_market table provisioning",
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_MarketBase.metadata.create_all)
        await _migrate_domain_to_tag(conn)
        await _migrate_publish_columns(conn)
    logger.info("ensured table agent_market")

    # 孤儿档案兜底清理（智能体已被删但市场档案残留的死数据）
    pruned = await prune_orphan_market_entries(storage)
    if pruned:
        logger.info("pruned %d orphan agent_market entries", pruned)

    # 存量回填：平台名下档案刷 published=1（发布机制上线前的老行）
    backfilled = await _backfill_platform_published(storage)
    if backfilled:
        logger.info("backfilled published=1 for %d platform entries", backfilled)

    # 默认标签补齐：平台名下还没有档案的智能体自动挂"未分类"
    tagged = await ensure_default_tags(storage)
    if tagged:
        logger.info("seeded default tag for %d platform agents", tagged)


async def _backfill_platform_published(storage: Any) -> int:
    """把平台名下智能体的市场档案刷成 ``published=1``（存量迁移兜底）。

    ``published`` 列上线前建的老档案该列默认 0，但平台智能体天然
    在市场，必须刷成 1；用户智能体不在这里动（它们本来就该是 0，
    由发布接口按需置 1）。系统内置智能体（``_`` 开头）没有档案行，
    天然被跳过。
    """
    from bocomadp.config.market_config import get_platform_user_id
    from agentscope.app.storage._sql._tables import AgentRow

    factory = _session_factory(storage)
    if factory is None:
        return 0
    platform = get_platform_user_id()
    async with factory() as session:
        platform_ids = {
            aid
            for (aid,) in (
                await session.execute(
                    select(AgentRow.id).where(AgentRow.user_id == platform),
                )
            ).all()
        }
        if not platform_ids:
            return 0
        rows = (
            await session.execute(
                select(AgentMarketRow).where(
                    AgentMarketRow.published.is_(False),
                    AgentMarketRow.agent_id.in_(platform_ids),
                ),
            )
        ).scalars().all()
        for row in rows:
            row.published = True
            row.updated_at = _now()
        if rows:
            await session.commit()
        return len(rows)


async def ensure_default_tags(storage: Any) -> int:
    """给平台名下**还没有市场档案**的智能体补默认标签（启动兜底）。

    平台智能体创建时自动建立市场档案，
    ``tag`` 恒不为空——"未分类"即为一种有效标签。运行期新建的由创建
    接口实时补（见 ``routers/agent.py`` 创建钩子），这里管历史存量
    与各路径遗漏。系统内置智能体（``_`` 开头 id）不参与——它们不进市场。
    """
    from bocomadp.config.market_config import get_default_tag, get_platform_user_id
    from agentscope.app.storage._sql._tables import AgentRow

    factory = _session_factory(storage)
    if factory is None:
        return 0
    platform = get_platform_user_id()
    default_tag = get_default_tag()
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentRow.id).where(AgentRow.user_id == platform),
            )
        ).all()
        existing = {
            aid
            for (aid,) in (
                await session.execute(select(AgentMarketRow.agent_id))
            ).all()
        }
        now = _now()
        added = 0
        for (aid,) in rows:
            if aid.startswith("_") or aid in existing:
                continue
            session.add(
                AgentMarketRow(
                    agent_id=aid,
                    tag=default_tag,
                    published=True,
                    created_at=now,
                    updated_at=now,
                ),
            )
            added += 1
        if added:
            await session.commit()
        return added


async def ensure_default_tag_for(storage: Any, agent_id: str) -> None:
    """单个智能体的默认标签补齐（平台创建接口钩子专用）。

    已有档案则不做任何改动（保护已配置的标签）；没有则写入默认标签。
    只有平台创建钩子会调用本函数，故新建档案 ``published=1``
    （平台智能体天然在市场）。
    """
    from bocomadp.config.market_config import get_default_tag

    if await get_market_entry(storage, agent_id) is not None:
        return
    await upsert_market_entry(
        storage,
        AgentMarketEntry(
            agent_id=agent_id,
            tag=get_default_tag(),
            published=True,
        ),
    )


async def get_market_entry(
    storage: Any,
    agent_id: str,
) -> AgentMarketEntry | None:
    """按 agent_id 取一条市场档案；不存在返回 None。"""
    factory = _session_factory(storage)
    if factory is None:
        return None
    async with factory() as session:
        row = await session.get(AgentMarketRow, agent_id)
        if row is None:
            return None
        return AgentMarketEntry(
            agent_id=row.agent_id,
            tag=row.tag,
            published=row.published,
            published_at=row.published_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


async def list_market_entries(storage: Any) -> list[AgentMarketEntry]:
    """全部市场档案（市场列表做 tag 关联用，量级 = 平台智能体数）。"""
    factory = _session_factory(storage)
    if factory is None:
        return []
    async with factory() as session:
        rows = (
            await session.execute(select(AgentMarketRow))
        ).scalars().all()
        return [
            AgentMarketEntry(
                agent_id=r.agent_id,
                tag=r.tag,
                published=r.published,
                published_at=r.published_at,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


async def upsert_market_entry(storage: Any, entry: AgentMarketEntry) -> None:
    """整行覆盖写（存在则更新，不存在则插入）。

    ``entry.created_at`` 为空时取当前时间（新建）；``updated_at``
    始终刷新为当前时间。
    """
    factory = _session_factory(storage)
    if factory is None:
        return
    now = _now()
    async with factory() as session:
        row = await session.get(AgentMarketRow, entry.agent_id)
        if row is None:
            session.add(
                AgentMarketRow(
                    agent_id=entry.agent_id,
                    tag=entry.tag,
                    published=entry.published,
                    published_at=entry.published_at,
                    created_at=entry.created_at or now,
                    updated_at=now,
                ),
            )
        else:
            # 只覆盖标签；发布状态归 set_market_published 管，不在这里动
            row.tag = entry.tag
            row.updated_at = now
        await session.commit()


async def set_market_published(
    storage: Any,
    agent_id: str,
    published: bool,
) -> AgentMarketEntry:
    """设置发布状态（``publish`` / ``unpublish`` 路由专用）。

    - 档案不存在则先建档（tag 取默认"未分类"），再置状态；
    - 发布：``published=1`` 且 ``published_at=now``（重复发布幂等，
      published_at 刷成当前时间）；
    - 撤回：``published=0`` 且 ``published_at`` 清空，**标签保留不动**。
    """
    from bocomadp.config.market_config import get_default_tag

    factory = _session_factory(storage)
    if factory is None:
        raise RuntimeError("storage has no session factory; cannot set published")
    now = _now()
    async with factory() as session:
        row = await session.get(AgentMarketRow, agent_id)
        if row is None:
            row = AgentMarketRow(
                agent_id=agent_id,
                tag=get_default_tag(),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        row.published = published
        row.published_at = now if published else None
        row.updated_at = now
        await session.commit()
        return AgentMarketEntry(
            agent_id=row.agent_id,
            tag=row.tag,
            published=row.published,
            published_at=row.published_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


async def delete_market_entry(storage: Any, agent_id: str) -> bool:
    """删除一条市场档案；删到返回 True，不存在返回 False。"""
    factory = _session_factory(storage)
    if factory is None:
        return False
    async with factory() as session:
        row = await session.get(AgentMarketRow, agent_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def prune_orphan_market_entries(storage: Any) -> int:
    """清理孤儿档案（agents 表里已经不存在的智能体）。

    删除智能体时路由层会级联删档案，但团队成员级联删除 / 手工删库等
    路径可能绕过，故启动时再兜底扫一遍，保证表里永远是活档案。
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
        rows = (await session.execute(select(AgentMarketRow))).scalars().all()
        orphans = [r for r in rows if r.agent_id not in alive]
        for row in orphans:
            await session.delete(row)
        if orphans:
            await session.commit()
        return len(orphans)


__all__ = [
    "AgentMarketEntry",
    "AgentMarketRow",
    "delete_market_entry",
    "ensure_default_tag_for",
    "ensure_default_tags",
    "ensure_market_tables",
    "get_market_entry",
    "list_market_entries",
    "prune_orphan_market_entries",
    "set_market_published",
    "upsert_market_entry",
]
