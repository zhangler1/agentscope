# -*- coding: utf-8 -*-
"""memory/extractor.py + memory/sweeper.py 单测：FakeRedis + 假 storage + 假平台。

- run_extract：窗口=turns、token 截断、过滤 user/assistant、保存重试、状态清理；
- MemorySweeper：静默会话扫描 → 抢锁 → run_extract。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agentscope.message import AssistantMsg, UserMsg

from bocomadp.memory import platform as pf
from bocomadp.memory import store as memory_store
from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.extractor import run_extract
from bocomadp.memory.state import (
    ACTIVE_ZSET,
    incr_turn,
    lock_key,
    mark_active,
    try_acquire_lock,
    turns_key,
)

from bocomadp.memory.store import MemoryConfig
from bocomadp.memory.sweeper import MemorySweeper


def _run(coro):
    return asyncio.run(coro)


def _chat_messages(n: int = 8, text: str = "content") -> list:
    msgs = []
    for index in range(n):
        content = f"{text}-{index} 内容"
        if index % 2 == 0:
            msgs.append(UserMsg(name="user", content=content))
        else:
            msgs.append(AssistantMsg(name="assistant", content=content))
    return msgs


class _FakeStorage:
    """假 storage：支持 list_messages（分页截断语义近似真实现）+ list_sessions。"""

    def __init__(self, messages=None, sessions=None):
        self.messages = messages or []
        self.sessions = sessions or []

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        del user_id, session_id
        if before is None:
            return list(self.messages[-limit:]), False
        # 简化：before 游标仅用于多页测试，这里直接返回空（单页场景足够）
        return [], False

    async def list_sessions(self, user_id, agent_id):
        del user_id, agent_id
        return list(self.sessions)


# ---------------------------------------------------------------------------
# run_extract
# ---------------------------------------------------------------------------


def test_extract_success_saves_and_cleans(fake_redis, monkeypatch):
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)
    msgs = _chat_messages()
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))  # W = 2 → 取最近 4 条消息
    storage = _FakeStorage(messages=msgs)
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    ok = _run(
        run_extract(
            fake_redis,
            storage,
            "u1",
            "a1",
            "s1",
            MemoryConfig(memory_enabled=True),
            MemoryRuntimeConfig(),
            recheck_active=False,
        ),
    )
    assert ok is True
    assert saved["param"]["agentId"] == "a1"
    assert saved["param"]["userCode"] == "u1"
    # 窗口 2 轮 = 最近 4 条（两条 user + 两条 assistant）
    assert len(saved["param"]["messages"]) == 4
    assert all(
        m["role"] in ("user", "assistant") and m["content"] for m in saved["param"]["messages"]
    )
    # 状态清理：turns 清零 + 提取锁释放
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert lock_key("u1", "a1", "s1") not in fake_redis._strings


def test_extract_aborts_when_session_still_active(fake_redis, monkeypatch):
    called = []

    async def _save(param):
        called.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=time.time()))  # 心跳最新 → 仍活跃
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    ok = _run(
        run_extract(
            fake_redis,
            _FakeStorage(messages=_chat_messages()),
            "u1",
            "a1",
            "s1",
            MemoryConfig(memory_enabled=True),
            MemoryRuntimeConfig(idle_minutes=15),
        ),
    )
    assert ok is False
    assert called == []
    # 锁仍被释放（放弃提取也放锁，供下次轮数/扫描再触发）
    assert lock_key("u1", "a1", "s1") not in fake_redis._strings


def test_extract_skips_when_no_turns(fake_redis, monkeypatch):
    called = []

    async def _save(param):
        called.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    ok = _run(
        run_extract(
            fake_redis,
            _FakeStorage(messages=_chat_messages()),
            "u1",
            "a1",
            "s1",
            MemoryConfig(memory_enabled=True),
            MemoryRuntimeConfig(),
            recheck_active=False,
        ),
    )
    assert ok is False
    assert called == []
    assert lock_key("u1", "a1", "s1") not in fake_redis._strings


def test_extract_save_retries_then_raises(fake_redis, monkeypatch):
    calls = []

    async def _fail(param):
        calls.append(param)
        raise pf.PlatformError("boom")

    monkeypatch.setattr(pf, "save_memories", _fail)
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    with pytest.raises(pf.PlatformError):
        _run(
            run_extract(
                fake_redis,
                _FakeStorage(messages=_chat_messages()),
                "u1",
                "a1",
                "s1",
                MemoryConfig(memory_enabled=True),
                MemoryRuntimeConfig(),
                recheck_active=False,
                retry_delays=(0.0, 0.0, 0.0),
            ),
        )
    assert len(calls) == 4  # 1 次初次尝试 + 3 次退避重试（1s/3s/9s）
    assert lock_key("u1", "a1", "s1") not in fake_redis._strings  # 失败也释放锁


def test_extract_truncates_to_max_tokens(fake_redis, monkeypatch):
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)
    # 每条 ~800 字节 ≈ 200 tokens；预算 500 → 从最旧丢弃直到 ≤500（保留最新 2 条）
    msgs = _chat_messages(n=8, text="x" * 800)
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    ok = _run(
        run_extract(
            fake_redis,
            _FakeStorage(messages=msgs),
            "u1",
            "a1",
            "s1",
            MemoryConfig(memory_enabled=True),
            MemoryRuntimeConfig(max_tokens=500),
            recheck_active=False,
        ),
    )
    assert ok is True
    assert len(saved["param"]["messages"]) == 2
    # 保留的是最新两条
    assert saved["param"]["messages"][-1]["content"].startswith("x" * 800)


# ---------------------------------------------------------------------------
# MemorySweeper
# ---------------------------------------------------------------------------


def test_sweeper_picks_idle_session(fake_redis, monkeypatch):
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=time.time() - 10000))  # 超静默窗口
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    storage = _FakeStorage(
        messages=_chat_messages(),
        sessions=[SimpleNamespace(id="s1")],
    )

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    sweeper = MemorySweeper(fake_redis, storage)
    attempts = _run(sweeper._sweep_once(MemoryRuntimeConfig(idle_minutes=15)))
    assert attempts == 1
    assert saved["param"]["messages"]


def test_sweeper_skips_active_session(fake_redis, monkeypatch):
    called = []

    async def _save(param):
        called.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=time.time()))  # 仍活跃 → 跳过
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    storage = _FakeStorage(
        messages=_chat_messages(),
        sessions=[SimpleNamespace(id="s1")],
    )

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    sweeper = MemorySweeper(fake_redis, storage)
    attempts = _run(sweeper._sweep_once(MemoryRuntimeConfig(idle_minutes=15)))
    assert attempts == 0
    assert called == []
