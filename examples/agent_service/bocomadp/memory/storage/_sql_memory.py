# -*- coding: utf-8 -*-
"""MemoryStorage 的 SQL 实现：2 张表 + payload JSON。

表结构（自建 ``DeclarativeBase``，不依赖框架私有 ``_tables._Base``）：

- ``memory_agent_configs``：agent_id PK + payload JSON；
- ``memory_session_states``：自增 id PK + 身份三元组唯一索引
  ``UK1_memory_session_states``（user_id, agent_id, session_id）+ 单列索引
  ``I1_memory_session_states``（last_active_at：清理与提取的范围查询 + 排序）
  与 ``I2_memory_session_states``（agent_id：按 agent 反查会话状态）+
  last_active_at（``DATETIME(6)``，naive UTC 字面值）+ turns（INT NOT NULL
  DEFAULT 0，提取候选的过滤列）+ payload；**提升列（user_id / agent_id /
  session_id / last_active_at / turns）只落列、payload 里剔除**，读时由
  列拼回（对齐框架 ``_JsonRecordMixin``「no field is ever stored in both
  places」契约，见 ``_SESSION_PROMOTED`` / ``_dump_session_payload`` /
  ``_load_session_record``）。

建表规范：每列带 COMMENT；两张表均含
created_at（``CURRENT_TIMESTAMP(6)``）与 updated_at（``CURRENT_TIMESTAMP(6)
ON UPDATE CURRENT_TIMESTAMP(6)``）；TEXT 列不做 NOT NULL；索引命名
``UK1_表名`` / ``I1_表名`` 且只建组合索引；时间列类型为 MySQL ``DATETIME(6)``。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import DATETIME as _MySQLDateTime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ._models import (
    HEARTBEAT_TZ,
    MemoryAgentConfigRecord,
    MemorySessionStateRecord,
)
from ._port import MemoryStorage

logger = logging.getLogger("as")


class _Base(DeclarativeBase):
    pass


class _EpochSeconds(TypeDecorator):
    """epoch 秒（float）↔ ``DATETIME``（naive UTC 字面值）的列适配。

    Python 侧保持端口契约的 float 秒，只在绑定/取值边界转换，业务代码
    无需感知时区与类型；写读成对使用同一固定偏移（UTC），故与服务器时区、
    数据库会话时区都无关，也与框架 ``_sql`` 的 naive UTC 约定一致。

    精度：列类型为 MySQL ``DATETIME(6)``（微秒级）——该列是会话心跳的
    唯一真源（payload 不含 ``last_active_at``），既服务索引范围查询
    （``last_active_before``），也供读路径拼回 record；float 往返截断到
    微秒（整秒无损）。
    """

    impl = _MySQLDateTime(fsp=6)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, tz=HEARTBEAT_TZ).replace(
            tzinfo=None
        )

    def process_result_value(self, value: Any, dialect: Any) -> float | None:
        if value is None:
            return None
        return value.replace(tzinfo=HEARTBEAT_TZ).timestamp()


class _AgentConfigRow(_Base):
    __tablename__ = "memory_agent_configs"
    __table_args__ = {
        "comment": "智能体记忆配置",
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }

    agent_id: Mapped[str] = mapped_column(
        String(255), primary_key=True, comment="智能体 ID"
    )
    payload: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="MemoryAgentConfigRecord JSON"
    )
    created_at: Mapped[datetime] = mapped_column(
        _MySQLDateTime(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        _MySQLDateTime(fsp=6),
        nullable=False,
        server_default=text(
            "CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"
        ),
        comment="更新时间",
    )


class _SessionStateRow(_Base):
    """会话状态行：自增主键 + 身份三元组唯一索引（唯一性由 UK1 保证）。"""

    __tablename__ = "memory_session_states"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "agent_id",
            "session_id",
            name="UK1_memory_session_states",
        ),
        Index("I1_memory_session_states", "last_active_at"),
        Index("I2_memory_session_states", "agent_id"),
        {
            "comment": "会话记忆状态（提取游标/轮数/心跳）",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="自增主键",
    )
    user_id: Mapped[str] = mapped_column(String(255), comment="用户 ID")
    agent_id: Mapped[str] = mapped_column(String(255), comment="智能体 ID")
    session_id: Mapped[str] = mapped_column(String(255), comment="会话 ID")
    last_active_at: Mapped[float] = mapped_column(
        _EpochSeconds,
        comment="最近活跃时间",
    )
    turns: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="累计轮数（提取触发计数；真源）",
    )
    payload: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="MemorySessionStateRecord JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        _MySQLDateTime(fsp=6),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(6)"),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        _MySQLDateTime(fsp=6),
        nullable=False,
        server_default=text(
            "CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"
        ),
        comment="更新时间",
    )


def _identity_clause(user_id: str, agent_id: str, session_id: str) -> tuple:
    """会话状态行的身份定位条件（与 uk 唯一索引同列）。"""
    return (
        _SessionStateRow.user_id == user_id,
        _SessionStateRow.agent_id == agent_id,
        _SessionStateRow.session_id == session_id,
    )


#: 会话状态读取的列集合：提升列（真源）+ payload（非提升字段）。
_SESSION_ROW_COLUMNS: tuple[Any, ...] = (
    _SessionStateRow.user_id,
    _SessionStateRow.agent_id,
    _SessionStateRow.session_id,
    _SessionStateRow.last_active_at,
    _SessionStateRow.turns,
    _SessionStateRow.payload,
)

#: ``delete_agent_session_states`` 的单批行数上限：限制单事务体积与锁窗口
#: （删 agent 是用户链路，不希望一个大 agent 撑出一个无界大事务）。
_DELETE_BATCH = 1000

#: 会话状态中「提升为独立列」的字段——payload 一律剔除、读时由列拼回
#: （对齐框架 ``_JsonRecordMixin``：no field is ever stored in both places）。
#: ``turns`` 参与提取候选过滤（``turns >= 1``），故必须是列。
_SESSION_PROMOTED: tuple[str, ...] = (
    "user_id",
    "agent_id",
    "session_id",
    "last_active_at",
    "turns",
)


def _dump_session_payload(record: MemorySessionStateRecord) -> str:
    """序列化会话状态 payload：剔除提升列（列才是真源）。"""
    return record.model_dump_json(exclude=set(_SESSION_PROMOTED))


def _load_session_record(
    payload: str | None,
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    last_active_at: float,
    turns: int,
) -> MemorySessionStateRecord:
    """由「payload（非提升字段）+ 提升列」拼回完整 record。

    提升列覆盖 payload（列是真源），故不依赖 payload 里是否残留旧值；payload
    为 NULL / 空串按空字典处理。
    """
    data = json.loads(payload) if payload else {}
    data.update(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        last_active_at=float(last_active_at),
        turns=int(turns),
    )
    return MemorySessionStateRecord.model_validate(data)


#: agent 配置中「提升为独立列」的字段（表 PK 是 agent_id）——payload 一律剔除、
#: 读时由列 / 入参拼回（§3.8，与 _SESSION_PROMOTED 同一契约）。
_AGENT_PROMOTED: tuple[str, ...] = ("agent_id",)


def _dump_agent_payload(record: MemoryAgentConfigRecord) -> str:
    """序列化 agent 配置 payload：剔除提升列（列才是真源）。"""
    return record.model_dump_json(exclude=set(_AGENT_PROMOTED))


def _load_agent_record(
    payload: str | None, *, agent_id: str
) -> MemoryAgentConfigRecord:
    """由「payload（非提升字段）+ agent_id 列 / 入参」拼回完整 record。"""
    data = json.loads(payload) if payload else {}
    data.update(agent_id=agent_id)
    return MemoryAgentConfigRecord.model_validate(data)


class SQLMemoryStorage(MemoryStorage):
    """SQL 后端（MySQL；测试同样打真实 MySQL，见 tests/conftest.py）。

    Args:
        db_url: SQLAlchemy 异步连接串；为空时回退 ``MemorySettings.db_url``。
        engine: 外部共享 ``AsyncEngine``；传入则本类不自建、``close()`` 不
            dispose（生命周期归调用方）。

    构造函数不做任何 IO；建表走显式 :meth:`ensure_ready`（幂等）。
    """

    def __init__(self, db_url: str = "", engine: Any = None) -> None:
        self._own_engine = engine is None
        if engine is not None:
            self._engine: AsyncEngine = engine
        else:
            if not db_url:
                from ..settings import get_settings

                db_url = get_settings().db_url
            if not db_url:
                raise ValueError(
                    "SQLMemoryStorage: db_url 与 engine 必须提供其一（fail-fast）"
                )
            if ":memory:" in db_url:
                # 内存库：单连接复用，跨 asyncio.run 边界的建表/查询同库
                from sqlalchemy.pool import StaticPool

                self._engine = create_async_engine(
                    db_url, poolclass=StaticPool
                )
            else:
                self._engine = create_async_engine(
                    db_url,
                    pool_size=2,
                    pool_pre_ping=True,
                    pool_recycle=3600,
                )
        self._ready = False
        # 单进程内串行化自增（FOR UPDATE 在 SQLite 方言被忽略；MySQL 跨
        # 进程仍由行锁保证——双层防护）
        self._turns_lock = asyncio.Lock()

    # -- 生命周期 -----------------------------------------------------------

    async def ensure_ready(self) -> None:
        """幂等建表（``CREATE TABLE IF NOT EXISTS`` 语义）。"""
        if self._ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        self._ready = True

    async def close(self) -> None:
        """释放自建 engine；共享 engine 不 dispose。"""
        if self._own_engine:
            await self._engine.dispose()

    # -- agent 级配置--------------------------------------------------

    async def upsert_memory_config(
        self, record: MemoryAgentConfigRecord
    ) -> MemoryAgentConfigRecord:
        async with self._engine.begin() as conn:
            payload = (
                await conn.execute(
                    select(_AgentConfigRow.payload).where(
                        _AgentConfigRow.agent_id == record.agent_id
                    )
                )
            ).scalar_one_or_none()
            data = _dump_agent_payload(record)
            if payload is None:
                await conn.execute(
                    insert(_AgentConfigRow).values(
                        agent_id=record.agent_id, payload=data
                    )
                )
            else:
                await conn.execute(
                    update(_AgentConfigRow)
                    .where(_AgentConfigRow.agent_id == record.agent_id)
                    .values(payload=data)
                )
        return record

    async def get_memory_config(
        self, agent_id: str
    ) -> MemoryAgentConfigRecord | None:
        async with self._engine.connect() as conn:
            payload = (
                await conn.execute(
                    select(_AgentConfigRow.payload).where(
                        _AgentConfigRow.agent_id == agent_id
                    )
                )
            ).scalar_one_or_none()
        if payload is None:
            return None
        return _load_agent_record(payload, agent_id=agent_id)

    async def list_memory_configs(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[MemoryAgentConfigRecord]:
        """按 agent_id 升序分页（默认 ``LIMIT 100``，避免无条件全表扫描）。"""
        stmt = (
            select(_AgentConfigRow.agent_id, _AgentConfigRow.payload)
            .order_by(_AgentConfigRow.agent_id)
            .limit(limit)
        )
        if offset:  # offset=0 时省去，保持 `... LIMIT :limit` 形态
            stmt = stmt.offset(offset)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            _load_agent_record(row.payload, agent_id=row.agent_id)
            for row in rows
        ]

    async def delete_memory_config(self, agent_id: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(_AgentConfigRow).where(_AgentConfigRow.agent_id == agent_id)
            )
        return bool(result.rowcount)

    # -- 会话状态 -------------------------------------------------------------

    async def touch_memory_session(
        self, user_id: str, agent_id: str, session_id: str, now: float
    ) -> MemorySessionStateRecord | None:
        """只更新已存在状态的 last_active_at；不存在 → None（不创建）。

        心跳只写提升列（payload 不含 ``last_active_at``）→ 不重写 JSON。
        """
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(*_SESSION_ROW_COLUMNS).where(*identity)
                )
            ).one_or_none()
            if row is None:
                return None
            await conn.execute(
                update(_SessionStateRow)
                .where(*identity)
                .values(last_active_at=now)
            )
            return _load_session_record(
                row.payload,
                user_id=row.user_id,
                agent_id=row.agent_id,
                session_id=row.session_id,
                last_active_at=now,
                turns=row.turns,
            )

    async def ensure_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> bool:
        """唯一创建入口：不存在才插入；已存在 no-op 返回 False。

        并发下两个请求可能同时判定"不存在"→ 由身份唯一索引兜底：
        撞唯一键视为已存在（返回 False），不覆盖他人写入。
        """
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._turns_lock, self._engine.begin() as conn:
            exists = (
                await conn.execute(
                    select(_SessionStateRow.id)
                    .where(*identity)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if exists is not None:
                return False
            record = MemorySessionStateRecord(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                turns=0,
                cursor_created_at=cursor_created_at,
                cursor_msg_id=cursor_msg_id,
                switch_epoch=epoch,
                last_active_at=now,
            )
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        insert(_SessionStateRow).values(
                            user_id=user_id,
                            agent_id=agent_id,
                            session_id=session_id,
                            last_active_at=now,
                            turns=0,
                            payload=_dump_session_payload(record),
                        )
                    )
            except IntegrityError:
                logger.debug(
                    "memory: ensure session %s hit identity conflict — "
                    "treated as existing",
                    session_id,
                )
                return False
            return True

    async def reset_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> MemorySessionStateRecord:
        """无条件重写（upsert）：turns=0 + 新游标 + 新 epoch + 心跳。"""
        identity = _identity_clause(user_id, agent_id, session_id)
        record = MemorySessionStateRecord(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            turns=0,
            cursor_created_at=cursor_created_at,
            cursor_msg_id=cursor_msg_id,
            switch_epoch=epoch,
            last_active_at=now,
        )
        payload = _dump_session_payload(record)
        async with self._engine.begin() as conn:
            exists = (
                await conn.execute(
                    select(_SessionStateRow.id).where(*identity)
                )
            ).scalar_one_or_none()
            if exists is None:
                try:
                    async with conn.begin_nested():
                        await conn.execute(
                            insert(_SessionStateRow).values(
                                user_id=user_id,
                                agent_id=agent_id,
                                session_id=session_id,
                                last_active_at=now,
                                turns=0,
                                payload=payload,
                            )
                        )
                except IntegrityError:
                    # 并发插入撞身份唯一键 → 退化为更新（upsert 语义）
                    await conn.execute(
                        update(_SessionStateRow)
                        .where(*identity)
                        .values(last_active_at=now, turns=0, payload=payload)
                    )
            else:
                await conn.execute(
                    update(_SessionStateRow)
                    .where(*identity)
                    .values(last_active_at=now, turns=0, payload=payload)
                )
        return record

    async def increment_memory_session_turns(
        self, user_id: str, agent_id: str, session_id: str
    ) -> int:
        """turns += 1 并刷 last_active_at；返回新值；**不重写 payload**。

        事务内 ``SELECT turns ... FOR UPDATE`` + ``UPDATE``（MySQL 无
        RETURNING，拿不到新值就无法做阈值判断 → 不能用裸
        ``UPDATE turns = turns + 1``）。**不存在则 no-op 返回 0**（不创建
        ——无游标状态会致提取回捞；正常流程 touch 保证存在，0 返回仅出现
        在竞态窗口，调用方忽略）。
        """
        identity = _identity_clause(user_id, agent_id, session_id)
        now = time.time()
        async with self._turns_lock, self._engine.begin() as conn:
            current = (
                await conn.execute(
                    select(_SessionStateRow.turns)
                    .where(*identity)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                return 0
            new_turns = int(current) + 1
            await conn.execute(
                update(_SessionStateRow)
                .where(*identity)
                .values(turns=new_turns, last_active_at=now)
            )
            return new_turns

    async def get_memory_session_state(
        self, user_id: str, agent_id: str, session_id: str
    ) -> MemorySessionStateRecord | None:
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(*_SESSION_ROW_COLUMNS).where(*identity)
                )
            ).one_or_none()
        if row is None:
            return None
        return _load_session_record(
            row.payload,
            user_id=row.user_id,
            agent_id=row.agent_id,
            session_id=row.session_id,
            last_active_at=row.last_active_at,
            turns=row.turns,
        )

    async def set_memory_session_cursor(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        created_at: str,
        msg_id: str,
    ) -> None:
        """行不存在时 no-op 记 debug（提取流程先 get 保证存在）。

        只写 payload（游标属非提升字段），不动心跳列。
        """
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(*_SESSION_ROW_COLUMNS).where(*identity)
                )
            ).one_or_none()
            if row is None:
                logger.debug(
                    "memory: set cursor on missing session %s — no-op",
                    session_id,
                )
                return
            record = _load_session_record(
                row.payload,
                user_id=row.user_id,
                agent_id=row.agent_id,
                session_id=row.session_id,
                last_active_at=row.last_active_at,
                turns=row.turns,
            ).model_copy(
                update={"cursor_created_at": created_at, "cursor_msg_id": msg_id}
            )
            await conn.execute(
                update(_SessionStateRow)
                .where(*identity)
                .values(payload=_dump_session_payload(record))
            )

    async def reset_memory_session_turns(
        self, user_id: str, agent_id: str, session_id: str, turns: int = 0
    ) -> None:
        """把 turns 置为 ``turns``（默认 0；cursor / 心跳不动）；行不存在则 no-op。

        直接改列（**不重写 payload**）：调用方依赖"置位失败只多触发、不丢
        不重"，故行缺失只记 debug、不抛错。
        """
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(_SessionStateRow).where(*identity).values(turns=turns)
            )
        if not result.rowcount:
            logger.debug(
                "memory: reset turns on missing session %s — no-op", session_id
            )

    async def delete_memory_session_state(
        self, user_id: str, agent_id: str, session_id: str
    ) -> bool:
        identity = _identity_clause(user_id, agent_id, session_id)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(_SessionStateRow).where(*identity)
            )
        return bool(result.rowcount)

    async def delete_stale_session_states(
        self, last_active_before: float, limit: int
    ) -> int:
        """两步点删，返回本批处理的候选数（≤ limit）。

        ① 无锁快照读挑出最老的 ``limit`` 个 id；
        ② 按主键等值点删。
        """
        async with self._engine.begin() as conn:
            ids = (
                await conn.execute(
                    select(_SessionStateRow.id)
                    .where(
                        _SessionStateRow.last_active_at < last_active_before
                    )
                    .order_by(_SessionStateRow.last_active_at)  # 最老优先防饥饿
                    .limit(limit)
                )
            ).scalars().all()
            if not ids:
                return 0
            result = await conn.execute(
                delete(_SessionStateRow).where(
                    _SessionStateRow.id.in_(ids),
                    _SessionStateRow.last_active_at < last_active_before,
                )
            )
        deleted = int(result.rowcount or 0)
        if deleted != len(ids):
            logger.debug(
                "memory: purge batch candidates=%d deleted=%d（差额=并发跳过）",
                len(ids),
                deleted,
            )
        return len(ids)

    async def delete_agent_session_states(self, agent_id: str) -> int:
        """分批点删该 agent 的全部会话状态，返回删除行数。
        """
        total = 0
        while True:
            async with self._engine.begin() as conn:
                ids = (
                    await conn.execute(
                        select(_SessionStateRow.id)
                        .where(_SessionStateRow.agent_id == agent_id)
                        .limit(_DELETE_BATCH)
                    )
                ).scalars().all()
                if not ids:
                    return total
                result = await conn.execute(
                    delete(_SessionStateRow).where(
                        _SessionStateRow.id.in_(ids),
                        _SessionStateRow.agent_id == agent_id,
                    )
                )
                total += len(ids)
            if int(result.rowcount or 0) != len(ids):
                logger.debug(
                    "memory: clean agent=%s batch candidates=%d deleted=%d",
                    agent_id,
                    len(ids),
                    result.rowcount,
                )

    async def list_memory_session_states(
        self,
        *,
        last_active_before: float,
        limit: int | None = None,
    ) -> list[MemorySessionStateRecord]:
        """提取候选（§3.5）：``turns >= 1`` 由 SQL 下推 + 心跳升序 + ``limit``。

        固定 ``ORDER BY last_active_at``取
        "最久静默优先"的前 N 条 —— 否则每轮可能取到同一批，造成后面的会话
        饥饿（``limit`` 由服务端截断）。
        """
        stmt = (
            select(*_SESSION_ROW_COLUMNS)
            .where(
                _SessionStateRow.last_active_at < last_active_before,
                _SessionStateRow.turns >= 1,
            )
            .order_by(_SessionStateRow.last_active_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [
            _load_session_record(
                row.payload,
                user_id=row.user_id,
                agent_id=row.agent_id,
                session_id=row.session_id,
                last_active_at=row.last_active_at,
                turns=row.turns,
            )
            for row in rows
        ]


__all__ = ["SQLMemoryStorage"]
