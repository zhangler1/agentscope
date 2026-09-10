# -*- coding: utf-8 -*-
"""共享 agent 多 user 验收：sweeper 静默提取使用每个会话的真实 owner。

场景：agent "shared" 配置归属者 u1；u1 与 u2 各自有会话
(u1:shared:s1 静默、u2:shared:s2 静默)。旧实现只枚举 u1 名下会话 → s2 永不提取；
修复后 sweeper 从 active_sessions 反查 owner，s2 以 owner=u2 被提取。
"""
from __future__ import annotations

import asyncio
import time

from agentscope.message import AssistantMsg, UserMsg

from bocomadp.memory import store as memory_store
from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.state import incr_turn, lock_key, mark_active
from bocomadp.memory.store import MemoryConfig
from bocomadp.memory.sweeper import MemorySweeper


def _run(coro):
    return asyncio.run(coro)


def _chat_messages(n: int = 6) -> list:
    msgs = []
    for index in range(n):
        if index % 2 == 0:
            msgs.append(UserMsg(name="user", content=f"u{index} 内容"))
        else:
            msgs.append(AssistantMsg(name="assistant", content=f"a{index} 内容"))
    return msgs


class _FakeStorage:
    """list_messages 按 (user, session) 区分：验证读取用的是真实 owner。"""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, str]] = []

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        del limit, before
        self.read_calls.append((user_id, session_id))
        return _chat_messages(), False


def test_sweeper_extracts_shared_agent_sessions_with_real_owner(fake_redis, monkeypatch):
    from bocomadp.memory import platform as pf

    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)

    # u1（配置归属者）与 u2 各自对 shared agent 有静默会话
    old = time.time() - 10000
    _run(mark_active(fake_redis, "u1", "shared", "s1", now=old))
    _run(incr_turn(fake_redis, "u1", "shared", "s1"))
    _run(mark_active(fake_redis, "u2", "shared", "s2", now=old))
    _run(incr_turn(fake_redis, "u2", "shared", "s2"))

    async def _fake_list_enabled():
        return [("shared", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)

    storage = _FakeStorage()
    sweeper = MemorySweeper(fake_redis, storage)
    attempts = _run(sweeper._sweep_once(MemoryRuntimeConfig(idle_minutes=15)))

    assert attempts == 2  # u1 与 u2 的静默会话都被提取
    assert ("u1", "s1") in storage.read_calls
    assert ("u2", "s2") in storage.read_calls
    # 平台 userCode 是真实 owner（最后一次保存对应 u2 会话）
    assert saved["param"]["userCode"] == "u2"
    # 两个会话锁均已释放
    assert lock_key("u1", "shared", "s1") not in fake_redis._strings
    assert lock_key("u2", "shared", "s2") not in fake_redis._strings
