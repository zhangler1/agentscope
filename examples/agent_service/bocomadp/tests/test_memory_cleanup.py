# -*- coding: utf-8 -*-
"""memory/__init__.py 生命周期函数单测：list_agent_session_ids / cleanup_agent_memory。

- list_agent_session_ids：按 agent 取会话 id（失败/无注入返回 []）；
- cleanup_agent_memory：删 DB 配置行 + Redis 会话态（best-effort 不抛错）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.memory import cleanup_agent_memory, list_agent_session_ids
from bocomadp.memory import store as memory_store
from bocomadp.memory.state import (
    ACTIVE_ZSET,
    incr_turn,
    lock_key,
    mark_active,
    try_acquire_lock,
    turns_key,
)
from bocomadp.memory.store import MemoryConfig


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(memory_store, "_get_engine", fake_engine)
    return engine


def test_cleanup_deletes_config_row_and_redis_state(sqlite_db, fake_redis):
    """删 DB 配置行 + 按 session_ids 清 Redis（active/turns/锁）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "s1", now=100.0))
    _run(incr_turn(fake_redis, "s1"))
    _run(try_acquire_lock(fake_redis, "s1"))

    _run(cleanup_agent_memory("u1", "a1", session_ids=["s1"], redis=fake_redis))

    # DB 配置行已删
    assert _run(memory_store.memory_get("u1", "a1")) is None
    # Redis：active 成员 / turns / 锁全部清除
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "s1")) is None
    assert _run(fake_redis.get(turns_key("s1"))) is None
    assert lock_key("s1") not in fake_redis._strings


def test_cleanup_without_redis_only_deletes_db_row(sqlite_db, fake_redis):
    """redis 未注入/未传 → 只删 DB 行；预置的 Redis 状态不动（调用方没给）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "s1", now=100.0))
    _run(incr_turn(fake_redis, "s1"))

    # 不传 redis：cleanup 内部取运行时 provider（测试环境未注入 → None）
    _run(cleanup_agent_memory("u1", "a1", session_ids=["s1"]))

    assert _run(memory_store.memory_get("u1", "a1")) is None
    # 未传 redis 时本函数不应触碰 Redis（由调用方决定是否传）
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "s1")) is not None


def test_cleanup_missing_row_and_empty_sessions_no_error(sqlite_db, fake_redis):
    """无配置行 / 无会话也绝不抛错。"""
    _run(cleanup_agent_memory("u1", "no-such", session_ids=[], redis=fake_redis))


class _FakeSessionsStorage:
    """假 storage：list_sessions 返回固定会话或抛错。"""

    def __init__(self, sessions=None, error=None):
        self._sessions = sessions or []
        self._error = error

    async def list_sessions(self, user_id, agent_id):
        del user_id, agent_id
        if self._error is not None:
            raise self._error
        return list(self._sessions)


def test_list_agent_session_ids_returns_ids():
    storage = _FakeSessionsStorage(
        sessions=[SimpleNamespace(id="s1"), SimpleNamespace(id="s2")],
    )
    assert _run(list_agent_session_ids("u1", "a1", storage=storage)) == ["s1", "s2"]


def test_list_agent_session_ids_error_returns_empty():
    storage = _FakeSessionsStorage(error=RuntimeError("boom"))
    assert _run(list_agent_session_ids("u1", "a1", storage=storage)) == []


def test_list_agent_session_ids_without_storage_returns_empty():
    """运行时 storage 未注入（测试环境默认 None）→ 返回 [] 不抛错。"""
    assert _run(list_agent_session_ids("u1", "a1")) == []
