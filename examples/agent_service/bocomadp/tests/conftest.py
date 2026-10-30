# -*- coding: utf-8 -*-
"""Shared pytest fixtures for bocomadp tests (no real Redis needed)."""
from __future__ import annotations

import re

import pytest


class _FakePipeline:
    """Accumulate pipelined commands, run them sequentially on execute()."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._commands: list[tuple[str, tuple]] = []

    def eval(self, *args):
        self._commands.append(("eval", args))
        return self

    def exists(self, *args):
        self._commands.append(("exists", args))
        return self

    async def execute(self) -> list:
        results = []
        for method, args in self._commands:
            fn = getattr(self._redis, method)
            results.append(await fn(*args))
        return results


class FakeRedis:
    """In-memory stand-in for the redis.asyncio client.

    Supports only the commands ConcurrencyGuard uses:
    incr / decr / hset / hdel / hgetall / exists / get / set / eval / pipeline
    — plus the sorted-set / delete primitives（zadd / zscore / zrem /
    zrangebyscore / delete）and ``set`` NX/EX flags.
    """

    def __init__(self) -> None:
        self._strings: dict[str, int] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    async def incr(self, key: str) -> int:
        self._strings[key] = self._strings.get(key, 0) + 1
        return self._strings[key]

    async def decr(self, key: str) -> int:
        self._strings[key] = self._strings.get(key, 0) - 1
        return self._strings[key]

    async def set(
        self,
        key: str,
        value: int | str,
        ex: int | None = None,
        nx: bool = False,
        exat: int | None = None,
    ) -> bool | None:
        """SET with optional NX / EX.  Simulates Redis semantics:
        success returns ``True``; ``NX`` on an existing key returns ``None``.
        """
        del ex, exat  # TTL not tracked in the in-memory stand-in
        exists = key in self._strings or key in self._hashes or key in self._zsets
        if nx and exists:
            return None
        self._strings[key] = value
        return True

    async def hset(self, key: str, field: str, value: str) -> int:
        h = self._hashes.setdefault(key, {})
        h[field] = value
        return 1

    async def hdel(self, key: str, field: str) -> int:
        h = self._hashes.get(key)
        if h is None or field not in h:
            return 0
        del h[field]
        if not h:
            # 模拟真实 Redis 语义:hash 最后一个 field 被删后 key 一并移除,
            # 否则空 hash 残留会让 exists() 误判 key 仍存在
            del self._hashes[key]
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._hashes.get(key, {}))

    async def hget(self, key: str, field: str) -> str | None:
        h = self._hashes.get(key)
        if h is None:
            return None
        return h.get(field)

    async def get(self, key: str) -> str | None:
        """当前字符串值(测试断言用)。"""
        val = self._strings.get(key)
        return str(val) if val is not None else None

    async def exists(self, *keys: str) -> int:
        return sum(
            1
            for k in keys
            if k in self._strings or k in self._hashes or k in self._zsets
        )

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            for store in (self._strings, self._hashes, self._zsets):
                if store.pop(key, None) is not None:
                    removed += 1
        return removed

    async def expire(self, key: str, seconds: int) -> bool:
        """TTL 不跟踪（测试替身）；真实 Redis 会按 TTL 清理 key。"""
        del key, seconds
        return True

    def _decr_floor(self, key: str) -> int:
        """DECR 并保底 0（与 Lua 的 decr_floor 同语义：计数不会退成负数）。"""
        self._strings[key] = self._strings.get(key, 0) - 1
        if self._strings[key] < 0:
            self._strings[key] = 0
        return self._strings[key]

    def _pop_hash_field(self, key: str, field: str) -> str | None:
        """删除哈希字段（hash 空了整体删除，模拟真实 Redis 语义）。"""
        h = self._hashes.get(key)
        if h is None or field not in h:
            return None
        value = h.pop(field)
        if not h:
            del self._hashes[key]
        return value

    async def _eval_release(self, args: tuple[str, ...]) -> int:
        """模拟 ``_RELEASE_SESSION_LUA``：run 结束释放名额。

        返回 1=已释放登记名额, 2=登记尚未发生(记提前结束标记), 0=无占位。
        """
        key, sid = args[0], args[1]
        sess_agent_key = "agentscope:running:session_agent"
        pend_key = f"agentscope:running:pending:{sid}"
        early_key = f"agentscope:running:early:{sid}"
        value = (self._hashes.get(key) or {}).get(sid)
        if value is not None:
            self._pop_hash_field(key, sid)
            self._decr_floor("agentscope:running:global")
            base = value.removesuffix(":seen")
            m = re.match(r"^(.+):\d+:\w+$", base) or re.match(r"^(.+):\d+$", base)
            if m:
                self._decr_floor(f"agentscope:running:user:{m.group(1)}")
            old_aid = self._pop_hash_field(sess_agent_key, sid)
            if old_aid:
                self._decr_floor(f"agentscope:running:agent:{old_aid}")
            self._strings.pop(pend_key, None)
            return 1
        pend = self._strings.get(pend_key)
        if pend is not None and int(pend) > 0:
            self._strings[early_key] = "1"
            return 2
        return 0

    async def _eval_rollback(self, args: tuple[str, ...]) -> int:
        """模拟 ``_ROLLBACK_SESSION_LUA``：回滚本轮占位（全量退 + 保底 0）。"""
        key, sid, user_id, agent_id = args[0], args[1], args[2], args[3]
        self._decr_floor("agentscope:running:global")
        if user_id:
            self._decr_floor(f"agentscope:running:user:{user_id}")
        if agent_id:
            self._decr_floor(f"agentscope:running:agent:{agent_id}")
        self._pop_hash_field(key, sid)
        self._pop_hash_field("agentscope:running:session_agent", sid)
        self._strings.pop(f"agentscope:running:pending:{sid}", None)
        self._strings.pop(f"agentscope:running:early:{sid}", None)
        return 1

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self._zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in z:
                added += 1
            z[member] = float(score)
        return added

    async def zscore(self, key: str, member: str) -> float | None:
        return self._zsets.get(key, {}).get(member)

    async def zrem(self, key: str, *members: str) -> int:
        z = self._zsets.get(key)
        if z is None:
            return 0
        removed = 0
        for member in members:
            if z.pop(member, None) is not None:
                removed += 1
        if not z:
            del self._zsets[key]
        return removed

    async def zrangebyscore(
        self,
        key: str,
        mn: float | str,
        mx: float | str = "+inf",
        withscores: bool = False,
    ) -> list:
        z = self._zsets.get(key, {})
        lo = -float("inf") if mn == "-inf" else float(mn)
        hi = float("inf") if mx == "+inf" else float(mx)
        items = [(m, s) for m, s in z.items() if lo <= s <= hi]
        items.sort(key=lambda t: t[1])
        if withscores:
            return items
        return [m for m, _ in items]

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    async def eval(self, script: str, numkeys: int, *args: str) -> int:
        """模拟 ConcurrencyGuard 使用的 Lua 脚本。

        - ``_RELEASE_SESSION_LUA``:run 结束释放名额——已登记则清登记并退
          三维计数(返回 1);仅占位未登记则记提前结束标记(返回 2)。
        - ``_ROLLBACK_SESSION_LUA``:回滚本轮占位——全量退三维计数并清
          待转正/提前结束标记。
        - ``_REGISTER_LUA``(含 HSET):条件注册——旧条目存在且锁 key 消失
          则先释放旧名额再覆盖写入,返回 1;run 提前结束则退占位不写入,
          返回 2;否则直接覆盖,返回 0。
        - ``_RECONCILE_LUA``(含 HGETALL,不含 HSET):整体对账——遍历注册表,
          判锁 → 判定候选(seen 立即 / grace 兜底)→ HDEL + DECR global +
          DECR user 原子,返回清理条数。
        无法识别的脚本抛 NotImplementedError。
        """
        if "ROLLBACK_SESSION" in script:
            return await self._eval_rollback(args)
        if "RELEASE_SESSION" in script:
            return await self._eval_release(args)
        if "HSET" in script:
            # _REGISTER_LUA:KEYS[1]=注册表, ARGV=[sid, 新value, 锁key前缀,
            # agent_id, user_id](伴随哈希 session_agent 同步维护 agent 维度计数)
            key, sid, new_value, lock_prefix = args[0], args[1], args[2], args[3]
            agent_id = args[4] if len(args) > 4 else ""
            user_id = args[5] if len(args) > 5 else ""
            sess_agent_key = "agentscope:running:session_agent"
            pend_key = f"agentscope:running:pending:{sid}"
            early_key = f"agentscope:running:early:{sid}"
            # 消费"待转正"计数
            pend = self._strings.get(pend_key)
            if pend is not None and int(pend) > 0:
                if int(pend) <= 1:
                    self._strings.pop(pend_key, None)
                else:
                    self._strings[pend_key] = int(pend) - 1
            # run 提前结束(释放先于登记到达)→ 退掉本轮占位,不写登记
            if early_key in self._strings:
                del self._strings[early_key]
                self._decr_floor("agentscope:running:global")
                if user_id:
                    self._decr_floor(f"agentscope:running:user:{user_id}")
                if agent_id:
                    self._decr_floor(f"agentscope:running:agent:{agent_id}")
                return 2
            h = self._hashes.get(key)
            old = h.get(sid) if h is not None else None
            if old is not None:
                lock_key = lock_prefix + sid
                if lock_key not in self._strings and lock_key not in self._hashes:
                    # 旧条目存在且旧对话已结束(锁消失)→ 释放旧名额
                    del h[sid]
                    self._decr_floor("agentscope:running:global")
                    m = re.match(r"^(.+):\d+:\w+$", old) or re.match(
                        r"^(.+):\d+$",
                        old,
                    )
                    if not m:
                        # 与真实 Lua 一致:先剥掉 :seen 后缀再解析 uid
                        m = re.match(r"^(.+):\d+:\w+$", old.removesuffix(":seen")) or re.match(
                            r"^(.+):\d+$",
                            old.removesuffix(":seen"),
                        )
                    if m:
                        self._decr_floor(f"agentscope:running:user:{m.group(1)}")
                    old_aid = self._pop_hash_field(sess_agent_key, sid)
                    if old_aid:
                        self._decr_floor(f"agentscope:running:agent:{old_aid}")
                    self._hashes.setdefault(key, {})[sid] = new_value
                    return 1
            self._hashes.setdefault(key, {})[sid] = new_value
            sess_agent = self._hashes.setdefault(sess_agent_key, {})
            if agent_id:
                sess_agent[sid] = agent_id
            else:
                sess_agent.pop(sid, None)
            return 0
        if "HGETALL" not in script:
            raise NotImplementedError(f"unrecognized Lua script: {script!r}")
        # _RECONCILE_LUA:KEYS[1]=注册表, ARGV[1]=锁 key 前缀, ARGV[2]=grace_secs,
        # ARGV[3]=now。整体对账语义与真实 Lua 一致,返回清理条数。
        key, lock_prefix, grace, now = args[0], args[1], args[2], args[3]
        grace = float(grace)
        now = float(now)
        h = self._hashes.get(key) or {}
        cleaned = 0
        for sid, value in list(h.items()):
            lock_key = lock_prefix + sid
            if lock_key in self._strings or lock_key in self._hashes:
                continue  # 锁 key 已出现(对话在跑)→ 跳过
            seen = False
            base = value
            if base.endswith(":seen"):
                seen = True
                base = base.removesuffix(":seen")
            m = re.match(r"^(.+):(\d+):\w+$", base) or re.match(
                r"^(.+):(\d+)$",
                base,
            )
            uid = m.group(1) if m else None
            ts = float(m.group(2)) if m else 0.0
            if seen or grace <= 0 or (now - ts) >= grace:
                del h[sid]
                self._decr_floor("agentscope:running:global")
                if uid:
                    self._decr_floor(f"agentscope:running:user:{uid}")
                aid = self._pop_hash_field("agentscope:running:session_agent", sid)
                if aid:
                    self._decr_floor(f"agentscope:running:agent:{aid}")
                cleaned += 1
        return cleaned


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


# ---------------------------------------------------------------------------
# SDK 版记忆包测试 fixtures（自 bocom 测试迁移）
#
# SQL 后端使用真实 MySQL：建表 DDL 为 MySQL 规范（DATETIME(6) /
# CURRENT_TIMESTAMP(6) / ON UPDATE），SQLite 建不出该结构；Redis 后端用
# fakeredis。未提供可达的 MySQL 时，SQL 相关用例自动 skip。
# ---------------------------------------------------------------------------
import os as _os
import socket as _socket
from urllib.parse import urlparse as _urlparse

from sqlalchemy import text as _sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import NullPool as _NullPool

from bocomadp.memory.storage._sql_memory import SQLMemoryStorage

SQL_DB_URL = _os.getenv(
    "MEMORY_TEST_DB_URL",
    "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope",
)

_TABLE_NAMES = (
    "memory_session_states",
    "memory_agent_configs",
)


def _mysql_reachable() -> bool:
    parsed = _urlparse(SQL_DB_URL.replace("mysql+aiomysql://", "mysql://"))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3306
    try:
        with _socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _require_mysql() -> None:
    if not _mysql_reachable():
        pytest.skip(f"MySQL not reachable: {SQL_DB_URL}")


class _IsolatedSQLStorage(SQLMemoryStorage):
    """测试专用 SQL 后端：建表前先 DROP 记忆三表，保证用例互不干扰。"""

    async def ensure_ready(self) -> None:
        async with self._engine.begin() as conn:
            for name in _TABLE_NAMES:
                await conn.execute(_sa_text(f"DROP TABLE IF EXISTS {name}"))
        await super().ensure_ready()


def _make_sql_storage() -> SQLMemoryStorage:
    _require_mysql()
    return _IsolatedSQLStorage(
        engine=_create_async_engine(SQL_DB_URL, poolclass=_NullPool)
    )


@pytest.fixture
def sql_db_url() -> str:
    return SQL_DB_URL


@pytest.fixture
def make_sql_storage():
    def _make():
        return _make_sql_storage()

    return _make


@pytest.fixture
def session_ids():
    async def _list(storage) -> list[str]:
        if hasattr(storage, "_redis"):
            members = await storage._redis.zrange(
                "agentscope:memory:sessions", 0, -1
            )
            return sorted(
                (m.decode() if isinstance(m, bytes) else m).split(":", 2)[2]
                for m in members
            )
        from sqlalchemy import select

        from bocomadp.memory.storage._sql_memory import _SessionStateRow

        async with storage._engine.connect() as conn:
            rows = (
                await conn.execute(select(_SessionStateRow.session_id))
            ).scalars().all()
        return sorted(rows)

    return _list


@pytest.fixture(params=["sql", "redis"])
def make_storage(request):
    if request.param == "sql":

        def _make():
            return _make_sql_storage()

    else:
        from fakeredis import aioredis as fakeredis_aio

        from bocomadp.memory.storage._redis_memory import RedisMemoryStorage

        def _make():
            return RedisMemoryStorage(
                fakeredis_aio.FakeRedis(), state_ttl_secs=86400 * 30
            )

    _make.backend = request.param  # type: ignore[attr-defined]
    return _make
