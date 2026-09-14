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

from bocomadp.memory import extractor as extractor_mod
from bocomadp.memory import platform as pf
from bocomadp.memory import store as memory_store
from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.extractor import run_extract
from bocomadp.memory.state import (
    ACTIVE_ZSET,
    KEY_PREFIX,
    cursor_key,
    get_cursor,
    incr_turn,
    lock_key,
    mark_active,
    set_cursor,
    try_acquire_lock,
    turns_key,
)

from bocomadp.memory.store import MemoryConfig
from bocomadp.memory.sweeper import MemorySweeper


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_dangling_wait(monkeypatch):
    """默认把「读前宽限」置 0：run_extract 的等待不应拖慢单测，用例按需覆盖。"""
    monkeypatch.setattr(extractor_mod, "_DANGLING_WAIT_SECS", 0.0)


def _msg(role: str, text: str, seq: int):
    """构造坐标确定的消息：created_at 递增、id 稳定，便于游标断言。"""
    factory = UserMsg if role == "user" else AssistantMsg
    return factory(
        name=role,
        content=text,
        created_at=f"2026-09-14T10:00:00.{seq:06d}",
        id=f"m{seq:04d}",
    )


def _chat_messages(n: int = 8, text: str = "content") -> list:
    msgs = []
    for index in range(n):
        content = f"{text}-{index} 内容"
        role = "user" if index % 2 == 0 else "assistant"
        msgs.append(_msg(role, content, index + 1))
    return msgs


class _FakeStorage:
    """假 storage：list_messages 复刻真实现（(created_at, msg_id) 排序 + before 分页）。"""

    def __init__(self, messages=None, sessions=None):
        # 保留调用方列表的别名：测试会往原列表追加新消息模拟后续对话
        self.messages = messages if messages is not None else []
        self.sessions = sessions or []

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        del user_id, session_id
        ordered = sorted(
            self.messages,
            key=lambda m: (m.created_at, m.id),
        )
        if before is not None:
            index = next(
                (i for i, m in enumerate(ordered) if m.id == before),
                None,
            )
            if index is None:
                return [], False
            ordered = ordered[:index]
        page = ordered[-limit:]
        return page, len(ordered) > limit

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
    # 失败不推进游标：下次重试窗口自动扩大（此处首次提取本无游标）
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) is None


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


# ---------------------------------------------------------------------------
# 游标增量 / 尾部悬空对齐 / 完整轮切片
# ---------------------------------------------------------------------------


def _cursor_of(msg) -> tuple[str, str]:
    return (msg.created_at, msg.id)


def test_state_keys_share_memory_prefix():
    """memory 包所有 Redis key 统一以 ``memory:`` 开头（state.py 是唯一来源）。"""
    keys = {
        ACTIVE_ZSET,
        turns_key("u1", "a1", "s1"),
        lock_key("u1", "a1", "s1"),
        cursor_key("u1", "a1", "s1"),
    }
    assert all(k.startswith(f"{KEY_PREFIX}:") for k in keys), keys
    # key 字面值保持稳定：线上已有状态不失效
    assert ACTIVE_ZSET == "memory:active_sessions"
    assert turns_key("u1", "a1", "s1") == "memory:turns:u1:a1:s1"
    assert lock_key("u1", "a1", "s1") == "memory:extract_lock:u1:a1:s1"
    assert cursor_key("u1", "a1", "s1") == "memory:cursor:u1:a1:s1"


def test_cursor_roundtrip_and_corrupt_value(fake_redis):
    """游标读写：无值/损坏值都视为无游标（回退兼容窗口）。"""
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) is None
    _run(set_cursor(fake_redis, "u1", "a1", "s1", "2026-09-14T10:00:00", "m1"))
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) == (
        "2026-09-14T10:00:00",
        "m1",
    )
    _run(fake_redis.set(cursor_key("u1", "a1", "s1"), "corrupt-no-sep"))
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) is None


def test_load_messages_after_pages_through_history(fake_redis):
    """增量跨多页（>50 条）时完整取回，且不含游标及更早的消息。"""
    msgs = _chat_messages(n=120)  # 60 轮
    cursor = _cursor_of(msgs[19])  # 游标落在第 20 条
    got = _run(
        extractor_mod._load_messages_after(
            _FakeStorage(messages=msgs),
            "u1",
            "s1",
            cursor,
        ),
    )
    assert len(got) == 100
    assert got[0].id == "m0021"
    assert got[-1].id == "m0120"


def test_extract_uses_cursor_for_incremental_window(fake_redis, monkeypatch):
    """有游标后只发增量：第二批不会重发第一批已提取的消息。"""
    batches = []

    async def _save(param):
        batches.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)

    msgs = [_msg("user", "问题-0", 1), _msg("assistant", "回答-0", 2)]
    storage = _FakeStorage(messages=msgs)

    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    assert _run(
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
    ) is True
    assert [m["content"] for m in batches[0]["messages"]] == ["问题-0", "回答-0"]
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) == _cursor_of(msgs[1])

    msgs.extend([_msg("user", "问题-1", 3), _msg("assistant", "回答-1", 4)])
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))
    assert _run(
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
    ) is True
    assert [m["content"] for m in batches[1]["messages"]] == ["问题-1", "回答-1"]


def test_extract_waits_before_reading_window(fake_redis, monkeypatch):
    """读窗口前先等 1 秒宽限：等待期内落库的 assistant 一次读就能拿到。"""
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)

    msgs = [
        _msg("user", "问题-0", 1),
        _msg("assistant", "回答-0", 2),
        _msg("user", "问题-1", 3),
    ]
    storage = _FakeStorage(messages=msgs)
    # 游标停在 A0 → 若立刻读，增量只有悬空的 U1（A1 尚未落库）
    _run(set_cursor(fake_redis, "u1", "a1", "s1", *_cursor_of(msgs[1])))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))

    slept: list[float] = []

    async def _fake_sleep(secs):
        # 模拟 ChatService 在这段宽限期内完成 assistant 落库
        slept.append(secs)
        storage.messages.append(_msg("assistant", "回答-1", 4))

    monkeypatch.setattr(extractor_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(extractor_mod, "_DANGLING_WAIT_SECS", 0.25)

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
    # 先按配置的宽限时长等待，只读一次窗口
    assert slept == [0.25]
    assert ok is True
    assert [m["role"] for m in saved["param"]["messages"]] == [
        "user",
        "assistant",
    ]
    assert saved["param"]["messages"][0]["content"] == "问题-1"
    # 游标推进到等待期内落库的 assistant（m0004）
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) == (
        "2026-09-14T10:00:00.000004",
        "m0004",
    )


def test_extract_truncates_dangling_tail_on_timeout(fake_redis, monkeypatch):
    """等待后 assistant 仍缺失（回复失败/实例崩溃）：截掉悬空 user 只发完整轮。"""
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)
    monkeypatch.setattr(extractor_mod, "_DANGLING_WAIT_SECS", 0.01)

    msgs = [
        _msg("user", "问题-0", 1),
        _msg("assistant", "回答-0", 2),
        _msg("user", "问题-1", 3),
        _msg("assistant", "回答-1", 4),
        _msg("user", "问题-2", 5),  # 悬空：A2 永远不会落库
    ]
    storage = _FakeStorage(messages=msgs)
    _run(set_cursor(fake_redis, "u1", "a1", "s1", *_cursor_of(msgs[1])))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
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
    assert [m["content"] for m in saved["param"]["messages"]] == [
        "问题-1",
        "回答-1",
    ]
    # 游标停在 A1：悬空的 U2 留待下一批
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) == _cursor_of(msgs[3])


def test_extract_keeps_multi_user_turn(fake_redis, monkeypatch):
    """一轮多条 user 输入：整轮保留（user 开头、assistant 结尾）。"""
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)

    msgs = [
        _msg("user", "问题-0", 1),
        _msg("assistant", "回答-0", 2),
        _msg("user", "输入-a", 3),
        _msg("user", "输入-b", 4),
        _msg("assistant", "回答-1", 5),
    ]
    storage = _FakeStorage(messages=msgs)
    _run(set_cursor(fake_redis, "u1", "a1", "s1", *_cursor_of(msgs[1])))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))

    assert _run(
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
    ) is True
    assert [m["role"] for m in saved["param"]["messages"]] == [
        "user",
        "user",
        "assistant",
    ]


def test_extract_drops_leading_orphan_assistant(fake_redis, monkeypatch):
    """上批因悬空只发了 user，本批增量以 assistant 开头 → 丢弃该孤立 assistant。"""
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)

    msgs = [
        _msg("user", "问题-0", 1),
        _msg("assistant", "回答-0", 2),
        _msg("user", "问题-1", 3),  # 上批只发到这条（A1 当时尚未落库）
        _msg("assistant", "回答-1", 4),
        _msg("user", "问题-2", 5),
        _msg("assistant", "回答-2", 6),
    ]
    storage = _FakeStorage(messages=msgs)
    _run(set_cursor(fake_redis, "u1", "a1", "s1", *_cursor_of(msgs[2])))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(try_acquire_lock(fake_redis, "u1", "a1", "s1"))

    assert _run(
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
    ) is True
    assert [m["content"] for m in saved["param"]["messages"]] == [
        "问题-2",
        "回答-2",
    ]


# ---------------------------------------------------------------------------
# 超龄清理（state_ttl_days）：sweeper 扫描时逐会话清除运行时状态
# ---------------------------------------------------------------------------


def test_sweeper_purges_stale_session_state(fake_redis, monkeypatch):
    """超龄会话：sweeper 清 active/turns/cursor 且不提取（遗忘后重新开始）。"""
    called = []

    async def _save(param):
        called.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)

    stale = time.time() - 8 * 86400  # 8 天前活跃 > 7 天保留窗口
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=stale))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))
    _run(set_cursor(fake_redis, "u1", "a1", "s1", "2026-09-01T10:00:00", "m1"))

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    sweeper = MemorySweeper(fake_redis, _FakeStorage(messages=_chat_messages()))
    attempts = _run(
        sweeper._sweep_once(
            MemoryRuntimeConfig(idle_minutes=15, state_ttl_days=7),
        ),
    )

    assert attempts == 0  # 超龄 → 不提取
    assert called == []
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is None
    assert _run(fake_redis.get(turns_key("u1", "a1", "s1"))) is None
    assert _run(get_cursor(fake_redis, "u1", "a1", "s1")) is None


def test_sweeper_keeps_session_within_retention(fake_redis, monkeypatch):
    """保留窗口内的静默会话照常提取（不被超龄清理误伤）。"""
    saved = {}

    async def _save(param):
        saved["param"] = param

    monkeypatch.setattr(pf, "save_memories", _save)

    _run(
        mark_active(
            fake_redis, "u1", "a1", "s1",
            now=time.time() - 6 * 86400,  # 6 天 < 7 天保留窗口
        ),
    )
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    storage = _FakeStorage(
        messages=_chat_messages(),
        sessions=[SimpleNamespace(id="s1")],
    )
    sweeper = MemorySweeper(fake_redis, storage)
    attempts = _run(
        sweeper._sweep_once(
            MemoryRuntimeConfig(idle_minutes=15, state_ttl_days=7),
        ),
    )

    assert attempts == 1
    assert saved["param"]["messages"]
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u1:a1:s1")) is None  # 提取成功已移除


# ---------------------------------------------------------------------------
# 扫描范围下推 / 单轮封顶 / 静默路径不等待宽限
# ---------------------------------------------------------------------------


def test_sweeper_only_takes_idle_sessions(fake_redis, monkeypatch):
    """需求 1：扫描只取静默成员，活跃会话不被传输、不被处理。"""
    saved = []

    async def _save(param):
        saved.append(param)

    monkeypatch.setattr(pf, "save_memories", _save)

    now = time.time()
    _run(mark_active(fake_redis, "u9", "a1", "s9", now=now))  # 活跃 → 不应被扫到
    for i in range(4):  # 静默（1~4 小时前活跃）
        _run(
            mark_active(
                fake_redis, f"u{i}", "a1", f"s{i}",
                now=now - (i + 1) * 3600,
            ),
        )
        _run(incr_turn(fake_redis, f"u{i}", "a1", f"s{i}"))

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    sweeper = MemorySweeper(fake_redis, _FakeStorage(messages=_chat_messages()))

    # 4 个静默会话一次性全部处理（提取成功即从活跃集移除）
    assert _run(sweeper._sweep_once(MemoryRuntimeConfig(idle_minutes=1))) == 4
    for i in range(4):
        assert _run(fake_redis.zscore(ACTIVE_ZSET, f"u{i}:a1:s{i}")) is None
    # 活跃会话压根没被取到（范围下推）
    assert _run(fake_redis.zscore(ACTIVE_ZSET, "u9:a1:s9")) is not None


def test_sweeper_extract_skips_dangling_grace(fake_redis, monkeypatch):
    """需求 3：静默扫描路径不等待 1 秒宽限（只有轮数触发才需要等）。"""
    sleeps: list[float] = []

    async def _fake_sleep(secs):
        sleeps.append(secs)

    monkeypatch.setattr(extractor_mod.asyncio, "sleep", _fake_sleep)

    async def _save(param):
        pass

    monkeypatch.setattr(pf, "save_memories", _save)
    _run(mark_active(fake_redis, "u1", "a1", "s1", now=time.time() - 3600))
    _run(incr_turn(fake_redis, "u1", "a1", "s1"))

    async def _fake_list_enabled():
        return [("a1", MemoryConfig(memory_enabled=True))]

    monkeypatch.setattr(memory_store, "memory_list_enabled", _fake_list_enabled)
    sweeper = MemorySweeper(fake_redis, _FakeStorage(messages=_chat_messages()))

    assert _run(sweeper._sweep_once(MemoryRuntimeConfig(idle_minutes=1))) == 1
    assert sleeps == []  # 静默路径全程无宽限等待
