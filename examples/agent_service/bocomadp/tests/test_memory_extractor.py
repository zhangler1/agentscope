# -*- coding: utf-8 -*-
"""memory/extractor.py 单测（Task 11，§7.4）：锁外落库确认 + 锁内五步序列。

形态：同步测试函数 + ``asyncio.run(scenario())`` 收口（与
test_memory_storage.py 一致）——``make_storage`` 工厂在场景协程内
``ensure_ready()``，MySQL / fakeredis 均绑定当次事件循环。

- confirm_persisted_then_run：正常路径 / 落库确认退避 / 穷尽不清零 /
  空窗口空转 / save 失败保状态 / 重试退避序列 (1, 3)；
- 纯函数（签名未变，自旧 test_memory_extract.py 迁入）：分页读窗 /
  预算截断 / 完整轮切片 / 游标判定。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agentscope.message import AssistantMsg, UserMsg

from bocomadp.memory import extractor as extractor_mod
from bocomadp.memory import platform as pf
from bocomadp.memory.config import MemoryGlobalConfig
from bocomadp.memory.extractor import _lock_key, confirm_persisted_then_run
from bocomadp.memory.settings import MemorySettings
from bocomadp.memory.storage._models import MemoryAgentConfigRecord


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast_settings(monkeypatch):
    """落库确认首查延迟 0.01s：不等真实 ≤7s 上限，退避序列仍可断言。"""
    monkeypatch.setattr(
        extractor_mod,
        "get_settings",
        lambda: MemorySettings(environment="sit", dangling_wait_secs=0.01),
    )


def _msg(role: str, text: str, seq: int):
    """构造坐标确定的消息：created_at 递增、id 稳定，便于游标断言。"""
    factory = UserMsg if role == "user" else AssistantMsg
    return factory(
        name=role,
        content=text,
        created_at=f"2026-09-30T10:00:00.{seq:06d}",
        id=f"m{seq:04d}",
    )


def _chat_messages(n: int = 8, text: str = "content") -> list:
    msgs = []
    for index in range(n):
        content = f"{text}-{index} 内容"
        role = "user" if index % 2 == 0 else "assistant"
        msgs.append(_msg(role, content, index + 1))
    return msgs


class _FakeFrameworkStorage:
    """假框架 storage：list_messages 复刻真实现（(created_at, msg_id) 排序
    + before 分页），供读窗与游标过滤链路使用。"""

    def __init__(self, messages=None):
        # 保留调用方列表别名：测试中途 append 模拟消息后续落库
        self.messages = messages if messages is not None else []

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        del user_id, session_id
        ordered = sorted(self.messages, key=lambda m: (m.created_at, m.id))
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


class _FakeBus:
    """假 message_bus：acquire_lock 以 asyncio.Lock 排队（场景协程内构造）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.keys: list[str] = []

    async def is_locked(self, key: str) -> bool:
        del key
        return self._lock.locked()

    @asynccontextmanager
    async def acquire_lock(self, key: str, ttl_secs: int = 600):
        del ttl_secs
        async with self._lock:
            self.keys.append(key)
            yield


async def _prepare_session(storage, *, turns: int = 1, cursor=None) -> None:
    """预置会话状态：ensure 建档 + turns 计数 + 可选游标 (created_at, msg_id)。

    同时把 agent 配置 upsert 进存储——锁内兜底会重读配置
    （``get_memory_config``），缺失会被判定 skip（需求方案 §6.5）。
    """
    await storage.upsert_memory_config(
        MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True)
    )
    cursor_created_at, cursor_msg_id = (
        cursor if cursor is not None else ("", "")
    )
    await storage.ensure_memory_session_state(
        "u1", "a1", "s1",
        cursor_created_at=cursor_created_at,
        cursor_msg_id=cursor_msg_id,
        epoch=0,
        now=1000.0,
    )
    for _ in range(turns):
        await storage.increment_memory_session_turns("u1", "a1", "s1")


async def _state(storage):
    return await storage.get_memory_session_state("u1", "a1", "s1")


def _fake_asyncio_sleep(sleeps: list[float], on_sleep=None):
    """替换 extractor 命名空间的 asyncio：记录 sleep 时长，可选回调模拟落库。"""

    async def _sleep(secs):
        sleeps.append(secs)
        if on_sleep is not None:
            on_sleep(sleeps)

    return SimpleNamespace(sleep=_sleep)


# ---------------------------------------------------------------------------
# ① 正常路径
# ---------------------------------------------------------------------------


def test_normal_path_saves_once_and_advances(make_storage, monkeypatch):
    """2 轮消息：save 一次、首尾 user/assistant、run_id=末条 assistant id、
    游标推进 + turns 清零、锁释放。"""
    saved = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        msgs = [
            _msg("user", "问题-0", 1),
            _msg("assistant", "回答-0", 2),
            _msg("user", "问题-1", 3),
            _msg("assistant", "回答-1", 4),
        ]
        framework = _FakeFrameworkStorage(messages=msgs)
        await _prepare_session(storage)  # turns=1，无游标（首次）
        bus = _FakeBus()
        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id="m0004",
        )
        assert ok is True
        # save 恰好一次；payload 为完整 2 轮（首 user 尾 assistant）
        assert len(saved) == 1
        req = saved[0]
        assert [(m.role, m.content) for m in req.messages] == [
            ("user", "问题-0"),
            ("assistant", "回答-0"),
            ("user", "问题-1"),
            ("assistant", "回答-1"),
        ]
        # D3：run_id = 本批末条被发送 assistant 消息 id
        assert req.run_id == "m0004"
        assert req.user_code == "u1"
        assert req.agent_id == "a1"
        # 第 5 步：游标推进 + turns 清零
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == "m0004"
        assert state.cursor_created_at == "2026-09-30T10:00:00.000004"
        assert state.turns == 0
        # 锁释放；锁键格式稳定（middleware 预检共用，Task 12）
        assert not await bus.is_locked(_lock_key("u1", "a1", "s1"))
        assert bus.keys == ["agentscope:memory:extract_lock:u1:a1:s1"]

    _run(scenario())


# ---------------------------------------------------------------------------
# ② 落库确认退避：首读空窗 → 第二次读到 → 不等满 3 次
# ---------------------------------------------------------------------------


def test_confirm_backoff_reads_until_persisted(make_storage, monkeypatch):
    """reply_id 首读未落库 → 退避 ×2 后读到即确认；增量窗口只发新落库的一轮。"""
    saved = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        msgs = [
            _msg("user", "问题-0", 1),
            _msg("assistant", "回答-0", 2),
            _msg("user", "问题-1", 3),  # A1 尚未落库
        ]
        framework = _FakeFrameworkStorage(messages=msgs)
        a0 = msgs[1]
        await _prepare_session(storage, cursor=(a0.created_at, a0.id))
        bus = _FakeBus()
        sleeps: list[float] = []

        def _persist_on_second(slept):
            if len(slept) == 2:
                # 第二次退避等待期：ChatService 完成 assistant 落库
                msgs.append(_msg("assistant", "回答-1", 4))

        monkeypatch.setattr(
            extractor_mod,
            "asyncio",
            _fake_asyncio_sleep(sleeps, on_sleep=_persist_on_second),
        )

        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id="m0004",
        )
        assert ok is True
        # 确认退避：0.01 → 0.02（第二次读到即中，无第三次 0.04）
        assert sleeps == [0.01, 0.02]
        assert [(m.role, m.content) for m in saved[0].messages] == [
            ("user", "问题-1"),
            ("assistant", "回答-1"),
        ]
        assert saved[0].run_id == "m0004"
        state = await _state(storage)
        assert state is not None and state.cursor_msg_id == "m0004"

    _run(scenario())


def test_confirm_reads_latest_one_only():
    """落库确认只为判断"末条 id == reply_id"，故每次只读最新 1 条
    （不按游标读整段增量、不传 before 翻页）。"""
    reads: list[tuple[int, str | None]] = []

    class _RecordingStorage(_FakeFrameworkStorage):
        async def list_messages(
            self, user_id, session_id, limit=50, before=None
        ):
            reads.append((limit, before))
            return await super().list_messages(
                user_id, session_id, limit=limit, before=before
            )

    async def scenario():
        msgs = _chat_messages(n=6)
        framework = _RecordingStorage(messages=msgs)
        ok = await extractor_mod._confirm_reply_persisted(
            framework, "u1", "s1", msgs[-1].id, 0.01
        )
        assert ok is True
        assert reads == [(1, None)]  # 单次读取、最新 1 条、无 before

    _run(scenario())


# ---------------------------------------------------------------------------
# ③ 确认穷尽 + 尾部悬空 → 不清零
# ---------------------------------------------------------------------------


def test_exhausted_confirm_dangling_tail_turns_one(make_storage, monkeypatch):
    """确认穷尽（reply_id 始终未落库）且切片末条 != reply_id：
    完整轮照发；游标照常推进到本批末条（结清已落库内容），
    turns 置 1（保留触发动力，下轮 / 静默把悬空那一轮补齐）。"""
    saved = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        msgs = [
            _msg("user", "问题-0", 1),
            _msg("assistant", "回答-0", 2),
            _msg("user", "问题-1", 3),
            _msg("assistant", "回答-1", 4),
            _msg("user", "问题-2", 5),  # 悬空：A2 永不落库
        ]
        framework = _FakeFrameworkStorage(messages=msgs)
        a0 = msgs[1]
        await _prepare_session(storage, turns=2, cursor=(a0.created_at, a0.id))
        bus = _FakeBus()
        sleeps: list[float] = []
        monkeypatch.setattr(
            extractor_mod, "asyncio", _fake_asyncio_sleep(sleeps)
        )

        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id="m0006",  # A2 的 id：始终未落库
        )
        assert ok is True  # 切片兜底：完整轮仍发送
        # 确认穷尽：0.01 / 0.02 / 0.04 三读全部落空（×2、×4 退避）
        assert sleeps == [0.01, 0.02, 0.04]
        # 截掉悬空 U2，只发完整轮 [U1, A1]
        assert [(m.role, m.content) for m in saved[0].messages] == [
            ("user", "问题-1"),
            ("assistant", "回答-1"),
        ]
        assert saved[0].run_id == "m0004"  # ≠ reply_id → turns 置 1
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == "m0004"  # 游标推进到本批末条（A1）
        assert state.turns == 1               # 置 1：保留补齐动力

    _run(scenario())


def test_dangling_turn_extracted_after_persist(make_storage, monkeypatch):
    """悬空轮补齐：上批已把游标推到 A1 且 turns=1；A2 落库后，
    下一批窗口 = [U2, A2] → 完整轮被提取、游标推进、turns 清零。"""
    saved = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        # 上批结果：游标停在 A1、turns=1（尾部 U2 那一轮待补齐）
        a1 = _msg("assistant", "回答-1", 4)
        await _prepare_session(storage, turns=1, cursor=(a1.created_at, a1.id))
        # A2 已落库（本轮回复）
        msgs = [
            _msg("user", "问题-2", 5),
            _msg("assistant", "回答-2", 6),
        ]
        framework = _FakeFrameworkStorage(messages=msgs)
        bus = _FakeBus()
        monkeypatch.setattr(
            extractor_mod, "asyncio", _fake_asyncio_sleep([])
        )

        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id="m0006",
        )
        assert ok is True
        assert [(m.role, m.content) for m in saved[0].messages] == [
            ("user", "问题-2"),
            ("assistant", "回答-2"),
        ]
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == "m0006"  # 游标补齐到 A2
        assert state.turns == 0                # 结清 → 清零

    _run(scenario())


# ---------------------------------------------------------------------------
# ④ 空窗口（游标已最新）→ save 不调、直接退出
# ---------------------------------------------------------------------------


def test_empty_window_skips_save(make_storage, monkeypatch):
    """空窗口：排队接续者的空转出口——save 不调、状态原样。"""
    called = []

    async def _save(request):
        called.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        msgs = [_msg("user", "问题-0", 1), _msg("assistant", "回答-0", 2)]
        framework = _FakeFrameworkStorage(messages=msgs)
        a0 = msgs[1]
        # 游标已停在窗口末条 → 增量为空
        await _prepare_session(storage, cursor=(a0.created_at, a0.id))
        bus = _FakeBus()
        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id=None,  # sweeper 场景：跳过确认
        )
        assert ok is False
        assert called == []
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == a0.id
        assert state.turns == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# ⑤ save 连续失败 3 击 → 游标 / turns 不动、锁释放
# ---------------------------------------------------------------------------


def test_save_failure_keeps_state_and_releases_lock(make_storage, monkeypatch):
    """save 连续失败：1 初次 + 2 重试共 3 击后上抛；游标 / turns 不动、锁释放。"""
    calls = []

    async def _fail(request):
        calls.append(request)
        raise pf.MemoriesSdkError("boom")

    monkeypatch.setattr(pf, "save_memories", _fail)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        framework = _FakeFrameworkStorage(messages=_chat_messages(n=4))
        await _prepare_session(storage)
        bus = _FakeBus()
        with pytest.raises(pf.MemoriesSdkError):
            await confirm_persisted_then_run(
                bus, storage, framework,
                "u1", "a1", "s1",
                MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
                MemoryGlobalConfig(),
                reply_id=None,
                retry_delays=(0.0, 0.0),
            )
        # 3 击（§7.4 行为变更：旧版 4 击）
        assert len(calls) == 3
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == ""  # 失败不推进游标
        assert state.turns == 1
        assert not await bus.is_locked(_lock_key("u1", "a1", "s1"))  # 异常路径也释放锁

    _run(scenario())


# ---------------------------------------------------------------------------
# ⑥ save 重试退避序列 (1, 3)
# ---------------------------------------------------------------------------


def test_save_retry_backoff_sequence(make_storage, monkeypatch):
    """默认退避 (1, 3)：两次失败第三次成功，恢复后照常推进游标 + 清零。"""
    # 常量守卫：默认退避序列即 (1.0, 3.0)
    assert extractor_mod._DEFAULT_RETRY_DELAYS == (1.0, 3.0)

    calls = []

    async def _flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise pf.MemoriesSdkError("flaky")

    monkeypatch.setattr(pf, "save_memories", _flaky)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        framework = _FakeFrameworkStorage(messages=_chat_messages(n=4))
        await _prepare_session(storage)
        bus = _FakeBus()
        sleeps: list[float] = []
        monkeypatch.setattr(
            extractor_mod, "asyncio", _fake_asyncio_sleep(sleeps)
        )

        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id=None,  # 不传 retry_delays → 用默认 (1.0, 3.0)
        )
        assert ok is True
        assert len(calls) == 3
        assert sleeps == [1.0, 3.0]  # 退避序列（fake sleep 不真等）
        state = await _state(storage)
        assert state is not None
        assert state.cursor_msg_id == "m0004"  # 成功后照常推进
        assert state.turns == 0

    _run(scenario())


# ---------------------------------------------------------------------------
# ⑥ 锁内 epoch 兜底：状态落后 → 就地重置到开关边界，只提取开启后内容
# ---------------------------------------------------------------------------


def test_locked_epoch_reset_then_extract(make_storage, monkeypatch):
    """epoch 落后（开关关过又开）：锁内重读配置 → 按本轮 user 前一条重置
    （游标 = 开关边界）→ 只提取开启后这一轮，关闭期内容不回捞。"""
    saved = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        msgs = [
            _msg("user", "关闭前-问题", 1),      # m0001
            _msg("assistant", "关闭前-回答", 2),  # m0002 ← 状态游标停在这
            _msg("user", "关闭期-问题", 3),      # m0003
            _msg("assistant", "关闭期-回答", 4),  # m0004 ← 开关边界
            _msg("user", "重开后-问题", 5),      # m0005 ← 本轮 user
            _msg("assistant", "重开后-回答", 6),  # m0006 ← 本轮回复
        ]
        framework = _FakeFrameworkStorage(messages=msgs)
        a0 = msgs[1]
        await _prepare_session(storage, turns=3, cursor=(a0.created_at, a0.id))
        # 开关关过又开：配置 epoch=2，而状态仍停在旧 epoch 0
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(
                agent_id="a1", extraction_enabled=True, switch_epoch=2
            )
        )
        bus = _FakeBus()
        monkeypatch.setattr(
            extractor_mod, "asyncio", _fake_asyncio_sleep([])
        )

        ok = await confirm_persisted_then_run(
            bus, storage, framework,
            "u1", "a1", "s1",
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            MemoryGlobalConfig(),
            reply_id="m0006",
            user_msg_id="m0005",
        )
        assert ok is True
        # 只发"开启后"的一轮：关闭期内容被重定位后的游标挡住
        assert [(m.role, m.content) for m in saved[0].messages] == [
            ("user", "重开后-问题"),
            ("assistant", "重开后-回答"),
        ]
        state = await _state(storage)
        assert state is not None
        assert state.switch_epoch == 2         # epoch 已同步
        assert state.cursor_msg_id == "m0006"  # 游标推进到本轮回复
        assert state.turns == 0

    _run(scenario())


# ---------------------------------------------------------------------------
# 纯函数（签名未变，自旧 test_memory_extract.py 迁入）
# ---------------------------------------------------------------------------


def test_load_recent_messages_takes_last_window():
    """无游标（首次）：取最近 want 条，全局正序（最旧在前）。"""
    msgs = _chat_messages(n=8)
    got = _run(
        extractor_mod._load_recent_messages(
            _FakeFrameworkStorage(messages=msgs), "u1", "s1", want=4
        )
    )
    assert [m.id for m in got] == ["m0005", "m0006", "m0007", "m0008"]


def test_load_messages_after_pages_through_history():
    """增量跨多页（>50 条）时完整取回，且不含游标及更早的消息。"""
    msgs = _chat_messages(n=120)  # 60 轮
    cursor = (msgs[68].created_at, msgs[68].id)  # 游标落在第 69 条
    got = _run(
        extractor_mod._load_messages_after(
            _FakeFrameworkStorage(messages=msgs), "u1", "s1", cursor, want=60
        )
    )
    assert len(got) == 51  # 游标后 51 条（> 单页 50 → 跨两页完整取回）
    assert got[0].id == "m0070"
    assert got[-1].id == "m0120"


def test_truncate_to_budget_drops_oldest():
    """超 token 预算：从最旧丢弃直到落回预算内（保留最新）。"""
    msgs = _chat_messages(n=8, text="x" * 800)  # 每条 ≈400 tokens（800 字节 / 2）
    kept = extractor_mod._truncate_to_budget(msgs, 500)
    assert len(kept) == 1  # 500 / ~400 → 只剩最新 1 条
    assert kept[-1].id == "m0008"


def test_slice_complete_turns_variants():
    """完整轮切片：多 user 轮整轮保留；头孤立 assistant / 尾悬空 user 截掉。"""
    # 一轮含多条 user（增量窗口）：整轮保留（user 开头、assistant 结尾）
    msgs = [
        _msg("user", "输入-a", 3),
        _msg("user", "输入-b", 4),
        _msg("assistant", "回答-1", 5),
    ]
    payload, last = extractor_mod._slice_complete_turns(msgs)
    assert [p["role"] for p in payload] == ["user", "user", "assistant"]
    assert last is not None and last.id == "m0005"

    # 头部孤立 assistant（上批悬空遗留）+ 尾部悬空 user：两头都截
    msgs2 = [
        _msg("assistant", "孤立-0", 1),
        _msg("user", "问题-1", 2),
        _msg("assistant", "回答-1", 3),
        _msg("user", "问题-2", 4),  # 悬空
    ]
    payload2, last2 = extractor_mod._slice_complete_turns(msgs2)
    assert payload2 == [
        {"role": "user", "content": "问题-1"},
        {"role": "assistant", "content": "回答-1"},
    ]
    assert last2 is not None and last2.id == "m0003"

    # 只有悬空 user → 无可发送
    payload3, last3 = extractor_mod._slice_complete_turns(
        [_msg("user", "问题", 1)]
    )
    assert payload3 == [] and last3 is None


def test_msg_pos_and_after_cursor():
    """游标判定：(created_at, id) 总序、None 游标全命中、严格大于。"""
    m1, m2 = _msg("user", "a", 1), _msg("assistant", "b", 2)
    assert extractor_mod._msg_pos(m1) < extractor_mod._msg_pos(m2)
    assert extractor_mod._after_cursor(m1, None) is True
    assert extractor_mod._after_cursor(m2, extractor_mod._msg_pos(m1)) is True
    assert extractor_mod._after_cursor(m1, extractor_mod._msg_pos(m1)) is False
    assert extractor_mod._after_cursor(m1, extractor_mod._msg_pos(m2)) is False
