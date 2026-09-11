# -*- coding: utf-8 -*-
"""memory/state.py + memory/middleware.py 单测：FakeRedis（conftest 扩展 zset/锁）。"""
from __future__ import annotations

import asyncio

from agentscope.message import AssistantMsg, UserMsg
from agentscope.state import AgentState

from bocomadp.memory.middleware import MemoryMiddleware
from bocomadp.memory.state import (
    clear_extract_state,
    encode_member,
    incr_turn,
    lock_key,
    mark_active,
    parse_member,
    try_acquire_lock,
    turns_key,
)
from bocomadp.memory.store import MemoryConfig


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# state.py —— Redis 原语
# ---------------------------------------------------------------------------


def test_incr_turn_auto_from_one(fake_redis):
    assert _run(incr_turn(fake_redis, "u1", "a1", "s1")) == 1
    assert _run(incr_turn(fake_redis, "u1", "a1", "s1")) == 2


def test_state_member_roundtrip():
    assert encode_member("u1", "a1", "s1") == "u1:a1:s1"
    assert parse_member("u1:a1:s1") == ("u1", "a1", "s1")


def test_state_primitives_roundtrip(fake_redis):
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=100.0))
    assert fake_redis._zsets["memory:active_sessions"]["u1:a1:s1"] == 100.0
    assert _run(try_acquire_lock(fake_redis, "u1", "a1", "s1")) is True
    assert _run(try_acquire_lock(fake_redis, "u1", "a1", "s1")) is False  # 已被占用
    _run(clear_extract_state(fake_redis, "u1", "a1", "s1"))
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert _run(fake_redis.zscore("memory:active_sessions", "u1:a1:s1")) is None


# ---------------------------------------------------------------------------
# middleware.py —— 触发判定 / 注入
# ---------------------------------------------------------------------------


def test_trigger_every_rounds():
    mw = MemoryMiddleware("u", "a", "s1", MemoryConfig(update_rounds=3), None)
    hits = []
    for n in range(1, 7):
        if mw._should_trigger(n):
            hits.append(n)
    assert hits == [3, 6]


def test_inject_appends_retains_old_memory():
    """仿 mem0：命中新记忆只追加，不删除历史记忆消息。"""
    class _Agent:
        def __init__(self):
            self.state = AgentState()

    a = _Agent()
    old = AssistantMsg(name="memory", content="old")
    a.state.context.append(old)
    a.state.context.append(UserMsg(name="user", content="hi"))
    mw = MemoryMiddleware("u", "a", "s1", MemoryConfig(), None)
    mw._inject_memories(a, ["new1", "new2"])
    memory_msgs = [m for m in a.state.context if getattr(m, "name", "") == "memory"]
    assert len(memory_msgs) == 2  # 旧记忆保留 + 本轮新追加一条
    assert "old" in memory_msgs[0].get_text_content()
    assert "new1" in memory_msgs[1].get_text_content()
    # 追加在 context 末尾（紧贴本轮 user 之后）
    assert a.state.context[-1] is memory_msgs[1]


def test_inject_empty_keeps_existing_memory():
    """mem0 语义：未命中（memories 为空）时不动 context，旧记忆保留。"""
    class _Agent:
        def __init__(self):
            self.state = AgentState()

    a = _Agent()
    old = AssistantMsg(name="memory", content="old")
    a.state.context.append(old)
    a.state.context.append(UserMsg(name="user", content="hi"))
    mw = MemoryMiddleware("u", "a", "s1", MemoryConfig(), None)
    mw._inject_memories(a, [])
    memory_msgs = [m for m in a.state.context if getattr(m, "name", "") == "memory"]
    assert len(memory_msgs) == 1  # 旧记忆仍在，无新增
    assert "old" in memory_msgs[0].get_text_content()


def test_trigger_once_until_lock_released(fake_redis):
    """达阈值触发一次；提取锁未释放前后续达阈值不再重复触发（防并发提取）。"""
    hits = []

    async def _trigger(turns):
        hits.append(turns)

    mw = MemoryMiddleware(
        "u",
        "a",
        "s1",
        MemoryConfig(update_rounds=3),
        None,
        redis=fake_redis,
        trigger=_trigger,
    )
    for _ in range(3):
        _run(mw._record_turn(None))
    assert hits == [3]
    for _ in range(3):
        # 第 6 轮也达阈值但提取锁仍占用（extractor 尚未清理）→ 不重复触发
        _run(mw._record_turn(None))
    assert hits == [3]
    assert lock_key("u", "a", "s1") in fake_redis._strings


def test_trigger_next_batch_after_extract_cleanup(fake_redis):
    """模拟 extractor 完整行为（清 turns + 释放锁）→ 后续每满 N 轮再次触发。"""
    hits = []

    async def _trigger(turns):
        hits.append(turns)
        # extractor 成功路径：clear_extract_state + 释放锁
        await clear_extract_state(fake_redis, "u", "a", "s1")
        await fake_redis.delete(lock_key("u", "a", "s1"))

    mw = MemoryMiddleware(
        "u",
        "a",
        "s1",
        MemoryConfig(update_rounds=3),
        None,
        redis=fake_redis,
        trigger=_trigger,
    )
    for _ in range(6):
        _run(mw._record_turn(None))
    # 每满 3 轮触发一次；turns 清零后重新计数，故两次触发值均为 update_rounds
    assert hits == [3, 3]
