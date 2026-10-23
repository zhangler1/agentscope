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

        - ``_REGISTER_LUA``(含 HSET):条件注册——旧条目存在且锁 key 消失
          则先释放旧名额再覆盖写入,返回 1;否则直接覆盖,返回 0。
        - ``_RECONCILE_LUA``(含 HGETALL,不含 HSET):整体对账——遍历注册表,
          判锁 → 判定候选(seen 立即 / grace 兜底)→ HDEL + DECR global +
          DECR user 原子,返回清理条数。
        无法识别的脚本抛 NotImplementedError。
        """
        if "HSET" in script:
            # _REGISTER_LUA:KEYS[1]=注册表, ARGV[1]=sid, ARGV[2]=新value,
            # ARGV[3]=锁 key 前缀
            key, sid, new_value, lock_prefix = args[0], args[1], args[2], args[3]
            h = self._hashes.get(key)
            old = h.get(sid) if h is not None else None
            if old is not None:
                lock_key = lock_prefix + sid
                if lock_key not in self._strings and lock_key not in self._hashes:
                    # 旧条目存在且旧对话已结束(锁消失)→ 释放旧名额
                    del h[sid]
                    self._strings["agentscope:running:global"] = (
                        self._strings.get("agentscope:running:global", 0) - 1
                    )
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
                        user_key = f"agentscope:running:user:{m.group(1)}"
                        self._strings[user_key] = self._strings.get(user_key, 0) - 1
                    self._hashes.setdefault(key, {})[sid] = new_value
                    return 1
            self._hashes.setdefault(key, {})[sid] = new_value
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
                self._strings["agentscope:running:global"] = (
                    self._strings.get("agentscope:running:global", 0) - 1
                )
                if uid:
                    user_key = f"agentscope:running:user:{uid}"
                    self._strings[user_key] = self._strings.get(user_key, 0) - 1
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
