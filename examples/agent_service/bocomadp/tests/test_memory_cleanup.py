# -*- coding: utf-8 -*-
"""memory/__init__.py 生命周期函数单测：cleanup_agent_memory。

- cleanup_agent_memory：DB 配置行同步删 + Redis 会话态**后台**清（best-effort 不抛错）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.memory import _pending_cleanup, cleanup_agent_memory
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


async def _cleanup_and_drain(agent_id: str, redis) -> None:
    """调用 cleanup 并等待其后台 Redis 清理任务完成（测试专用）。"""
    await cleanup_agent_memory(agent_id, redis=redis)
    pending = list(_pending_cleanup)
    if pending:
        await asyncio.gather(*pending)
    await asyncio.sleep(0)  # 让 done_callback 摘除 _pending_cleanup


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(memory_store, "_get_engine", fake_engine)
    return engine


def test_cleanup_deletes_config_row_and_redis_state(sqlite_db, fake_redis):
    """DB 配置行已删 + 后台任务清 Redis（active/turns/锁）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=100.0))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))

    _run(_cleanup_and_drain("a1", fake_redis))

    # DB 配置行已删
    assert _run(memory_store.memory_get("a1")) is None
    # Redis：active 成员 / turns / 锁全部清除
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is None
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert lock_key("u1", "a1", "s1") not in fake_redis._strings


def test_cleanup_clears_all_owners_of_shared_agent(sqlite_db, fake_redis):
    """共享 agent：按 agent 清掉所有 owner 的 Redis 会话态（不止调用者）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=100.0))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(mark_active(fake_redis, "u2", "a1", "s2", now=100.0))
    _run(incr_turn(fake_redis, "u2", "a1", "s2"))
    # 另一个 agent 的状态不应被误删
    _run(mark_active(fake_redis, "u1", "a2", "s9", now=100.0))
    _run(incr_turn(fake_redis, "u1", "a2", "s9"))

    _run(_cleanup_and_drain("a1", fake_redis))

    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is None
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u2:a1:s2")) is None
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert _run(fake_redis.get(turns_key("u2", "a1", "s2"))) is None
    # 其它 agent 保留
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a2:s9")) is not None
    assert _run(fake_redis.get(turns_key("u1", "a2", "s9"))) == "1"


def test_cleanup_without_redis_only_deletes_db_row(sqlite_db, fake_redis):
    """redis 未注入/未传 → 只删 DB 行；预置的 Redis 状态不动（调用方没给）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=100.0))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))

    # 不传 redis：cleanup 内部取运行时 provider（测试环境未注入 → None）
    _run(cleanup_agent_memory("a1"))
    _run(asyncio.sleep(0))

    assert _run(memory_store.memory_get("a1")) is None
    # 未传 redis 时不调度后台任务、不触碰 Redis
    assert not _pending_cleanup
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is not None


def test_cleanup_missing_row_and_empty_sessions_no_error(sqlite_db, fake_redis):
    """无配置行 / 无活跃会话也绝不抛错。"""
    _run(_cleanup_and_drain("no-such", fake_redis))
    assert not _pending_cleanup


def test_cleanup_redis_is_background_db_is_sync(sqlite_db, fake_redis):
    """Redis 清理为后台任务：cleanup 返回时 DB 已删、Redis 尚未清（任务待执行）。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=100.0))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))

    async def _scenario():
        await cleanup_agent_memory("a1", redis=fake_redis)
        # ① Redis 清理已调度为后台任务（此处未让出事件循环，任务尚未执行）
        scheduled = list(_pending_cleanup)
        assert len(scheduled) == 1
        assert fake_redis._zsets.get(ACTIVE_ZSET, {}).get("u1:a1:s1") is not None
        # ② DB 行同步已删（cleanup 返回前已完成）
        assert await memory_store.memory_get("a1") is None
        # ③ 等待后台任务完成
        await asyncio.gather(*scheduled)
        await asyncio.sleep(0)

    _run(_scenario())

    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is None
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert not _pending_cleanup
