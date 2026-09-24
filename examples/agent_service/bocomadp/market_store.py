# -*- coding: utf-8 -*-
"""智能体市场名单表（``agent_market``）+ 异步访问层。

与 ``team_store.py`` 同一套模式：bocomadp 自建 declarative base，复用框架
存储的 ``_engine`` / ``_session_factory``，启动时 ``ensure_market_tables``
幂等建表，``src/`` 框架零改动。

表语义（**名单表**，一行 = 一个市场智能体，有行 = 在市场）：

- ``agent_id``    主键，即 ``agents.id``（1:1，删智能体级联删行）；
- ``tag``         自由标签（≤64 字符），空串 = 未打标；打标/撕标仅限
  名单内智能体，撕标即把 ``tag`` 置空；
- ``created_at`` / ``updated_at`` 时间戳。

名单来源（两类，代码一律不自动补档）：

- 平台内置智能体：运营手动 INSERT（``agent_id`` 填 ``agents.id``）；
- 个人智能体：owner 调 ``POST /agent/market/{id}/publish`` 插行，
  ``/unpublish`` 删行（标签随行消失，重新发布重打）。

历史口径（``user_id=default`` = 平台智能体、``published`` 判定市场
范围）已废弃。生产部署时 ``agent_market`` 一律按新结构**新建**，不
存在存量迁移；本地老库若残留旧口径的 ``published`` / ``published_at``
列也不影响运行（ORM 只读写字段定义内的列，多余列走列默认值），
需要清理时手工执行即可（见 docs/agent-market-api.md 运营手册）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import DateTime, Index, String, select
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
    # 发布弹窗档案：publish 时随申请填写，approve 上架时从审批行复制
    # 过来，市场列表/精选直接展示。业务条线不单独设列——
    # 就是本表的 tag（打标/撕标接口直接复用）。列名统一 system_name
    # （比 system 更达意：存的是"系统名"，且避免与泛指"系统"混淆），
    # 与接口字段同名免转换。
    department: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    system_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)

    __table_args__ = (Index("ix_agent_market_tag", "tag"),)


# ---------------------------------------------------------------------------
# 业务模型
# ---------------------------------------------------------------------------


class AgentMarketEntry(BaseModel):
    """一条市场名单记录（对应表里的一行）。"""

    agent_id: str
    tag: str = ""
    department: str = ""
    system_name: str = ""
    description: str = ""
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


#: 列级迁移清单：表名 → {列名: DDL}。老库已建过表，``create_all``
#: 只按表名跳过不会补新列，ORM 查询全列 SELECT 会报 Unknown column，
#: 故启动时逐列检查、缺列补 ``ALTER TABLE ADD COLUMN``。
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "agent_market": {
        "department": "VARCHAR(64) NOT NULL DEFAULT ''",
        "system_name": "VARCHAR(64) NOT NULL DEFAULT ''",
        "description": "VARCHAR(500) NOT NULL DEFAULT ''",
    },
}


async def ensure_columns(engine: Any, migrations: dict[str, dict[str, str]]) -> None:
    """列级幂等迁移（MySQL / SQLite 通用）：缺列才 ALTER，已有列不动。

    ``market_review_store.ensure_review_tables`` 也复用本函数（审批表
    同批新增的 4 列），放这里避免两个 store 各抄一份。
    """
    from sqlalchemy import inspect

    async with engine.begin() as conn:

        def _existing(sync_conn: Any) -> dict[str, set[str]]:
            insp = inspect(sync_conn)
            return {
                t: {c["name"] for c in insp.get_columns(t)}
                for t in migrations
            }

        tables = await conn.run_sync(_existing)
        for table, cols in migrations.items():
            for name, ddl in cols.items():
                if name in tables.get(table, set()):
                    continue
                await conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl}",
                )
                logger.info("added column %s to %s", name, table)


async def ensure_market_tables(storage: Any) -> None:
    """启动时建表（幂等，已存在则跳过）+ 列级迁移 + 孤儿名单清理。

    生产部署时 ``agent_market`` 按新结构新建，无存量迁移；本地老库
    残留的旧口径列不影响运行，需要清理走手工 SQL（见运营手册）。
    """
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning(
            "storage has no _engine yet; skip agent_market table provisioning",
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_MarketBase.metadata.create_all)
    await ensure_columns(engine, _COLUMN_MIGRATIONS)
    logger.info("ensured table agent_market")

    # 孤儿名单兜底清理（智能体已被删但市场行残留的死数据）
    pruned = await prune_orphan_market_entries(storage)
    if pruned:
        logger.info("pruned %d orphan agent_market entries", pruned)


async def insert_market_entry(
    storage: Any,
    agent_id: str,
    tag: str = "",
    meta: dict[str, str] | None = None,
) -> bool:
    """把一个智能体放进市场名单（publish 专用）。

    不存在则插行（``tag`` 默认空串 = 未打标），返回 True；
    已在名单内则**不动已有行**（幂等，不覆盖已配置的标签），返回 False。

    ``meta`` 是发布档案（approve 上架时从审批行复制过来）：
    ``{"department", "system_name", "description"}``，缺键兜底空串；
    业务条线不进 meta——走 ``tag`` 参数（业务条线即 tag）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return False
    now = _now()
    meta = meta or {}
    async with factory() as session:
        if await session.get(AgentMarketRow, agent_id) is not None:
            return False
        session.add(
            AgentMarketRow(
                agent_id=agent_id,
                tag=tag,
                department=meta.get("department", ""),
                system_name=meta.get("system_name", ""),
                description=meta.get("description", ""),
                created_at=now,
                updated_at=now,
            ),
        )
        await session.commit()
        return True


async def set_market_tag(storage: Any, agent_id: str, tag: str) -> bool:
    """给名单内智能体打标/撕标（``tag`` 传空串 = 撕标）。

    仅更新 ``tag`` 与 ``updated_at``；智能体不在市场名单内返回 False
    （打标不允许隐式上架——上架只能走运营手动 INSERT 或 publish）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return False
    async with factory() as session:
        row = await session.get(AgentMarketRow, agent_id)
        if row is None:
            return False
        row.tag = tag
        row.updated_at = _now()
        await session.commit()
        return True


async def get_market_entry(
    storage: Any,
    agent_id: str,
) -> AgentMarketEntry | None:
    """按 agent_id 取一条市场名单记录；不在名单内返回 None。"""
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
            department=row.department,
            system_name=row.system_name,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


async def list_market_entries(storage: Any) -> list[AgentMarketEntry]:
    """全部市场名单（市场列表做 JOIN / tag 关联用）。"""
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
                department=r.department,
                system_name=r.system_name,
                description=r.description,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


async def list_market_tags(storage: Any) -> list[str]:
    """市场已使用的标签清单（去重、升序，空串=未打标不进清单）。

    数据源就是 ``agent_market.tag`` 现存量——自由标签口径下不存在
    预设清单（旧版 ``config.yaml`` 的 ``domains`` 已随 37 课删除），
    前端筛选下拉框直接吃这个。
    """
    factory = _session_factory(storage)
    if factory is None:
        return []
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentMarketRow.tag)
                .where(AgentMarketRow.tag != "")
                .distinct(),
            )
        ).scalars().all()
    return sorted(rows)


async def delete_market_entry(storage: Any, agent_id: str) -> bool:
    """把智能体移出市场名单（unpublish / 删除级联共用）。

    删到返回 True，本来就不在名单内返回 False。
    """
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
    """清理孤儿名单行（agents 表里已经不存在的智能体）。

    删除智能体时路由层会级联删名单行，但团队成员级联删除 / 手工删库等
    路径可能绕过，故启动时再兜底扫一遍，保证表里永远是活名单。
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
    "ensure_market_tables",
    "get_market_entry",
    "insert_market_entry",
    "list_market_entries",
    "list_market_tags",
    "prune_orphan_market_entries",
    "set_market_tag",
]
