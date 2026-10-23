# -*- coding: utf-8 -*-
"""memory/middleware.py 单测（Task 12，§8/§9）：触发节律 + 后台化 + 检索注入。

形态：同步测试函数 + ``asyncio.run(scenario())`` 收口（与
test_memory_storage.py 一致）。触发链路的 confirm_persisted_then_run
统一 monkeypatch 为可控 fake（真链路行为由 test_memory_extractor.py
覆盖）；状态读写走 make_storage 双后端工厂（协程内 ensure_ready）。

- 触发节律：turns 达 update_rounds 触发一次、未到不触发；
- ``>=`` 语义（§7.4 行为变更）：提取失败不清零 → 第 11 轮即再触发；
- is_locked 预检：已锁不 spawn（下轮重试，不丢数据）；
- 续轮只 touch 不计（update_rounds 统计用户交互轮数）；
- create_task 后台化：_record_turn 立即返回、_pending_extract 登记 +
  done 回调摘除、reply_id 透传（吸收旧 test_memory_background.py）；
- 检索默认路径走 mapping+platform（请求对象单一真源）。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from agentscope.message import AssistantMsg, HintBlock, UserMsg

from bocomadp.memory import middleware as middleware_mod
from bocomadp.memory import platform as pf
from bocomadp.memory.config import MemoryGlobalConfig
from bocomadp.memory.middleware import MemoryMiddleware
from bocomadp.memory.storage._models import MemoryAgentConfigRecord


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_pending():
    """隔离模块级任务登记表：前后清空，防止用例间串扰。"""
    middleware_mod._pending_extract.clear()
    yield
    middleware_mod._pending_extract.clear()


def _msg(role: str, text: str, seq: int):
    factory = UserMsg if role == "user" else AssistantMsg
    return factory(
        name=role,
        content=text,
        created_at=f"2026-09-30T10:00:00.{seq:06d}",
        id=f"m{seq:04d}",
    )


class _Agent:
    """假 agent：state.context（消息历史）+ state.reply_id（提取确认锚点）。

    契约与真实 ``agentscope.agent.Agent`` 对齐——2.0 没有 ``memory``
    属性，消息历史在 ``state.context``（``AgentState.context``）。
    """

    def __init__(self, reply_id: str = "r-1") -> None:
        self.state = SimpleNamespace(reply_id=reply_id, context=[])


class _FakeBus:
    """假 message_bus：is_locked / acquire_lock 以 asyncio.Lock 实现。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def is_locked(self, key: str) -> bool:
        del key
        return self._lock.locked()

    def acquire_lock(self, key: str, ttl_secs: int = 600):
        del key, ttl_secs
        return self._ctx()

    def _ctx(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _inner():
            async with self._lock:
                yield

        return _inner()


class _FakeFwStorage:
    """假框架存储：list_messages 返回单条 user 消息（游标定位锚点）。"""

    def __init__(self, msgs=None) -> None:
        self.msgs = msgs if msgs is not None else [_msg("user", "q", 1)]

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        del user_id, session_id, limit, before
        return list(self.msgs), False


def _mw(
    record: MemoryAgentConfigRecord,
    *,
    memory_storage=None,
    storage=None,
    message_bus=None,
) -> MemoryMiddleware:
    """构造被测中间件（注入用例的依赖占位 None——不触达状态链路）。"""
    return MemoryMiddleware(
        "u1",
        "a1",
        "s1",
        record,
        MemoryGlobalConfig(),
        memory_storage=memory_storage,
        storage=storage,
        message_bus=message_bus,
    )


# ---------------------------------------------------------------------------
# 触发节律与 >= 语义
# ---------------------------------------------------------------------------


def test_trigger_at_threshold_and_ge_semantics(make_storage, monkeypatch):
    """turns 达 update_rounds 触发一次；提取失败未清零 → 第 11 轮即再触发
    （§7.4 行为变更：>= 替代取模，不必等 20）。"""
    spawned: list[dict] = []

    async def _fake_extract(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        return False  # 模拟提取失败（turns/游标都不动）

    monkeypatch.setattr(
        middleware_mod, "confirm_persisted_then_run", _fake_extract
    )

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1", extraction_enabled=True, update_rounds=10
            ),
            memory_storage=storage,
            storage=_FakeFwStorage(),
            message_bus=_FakeBus(),
        )
        # 前 9 轮：计数但未达阈值，不触发
        for i in range(9):
            await mw._record_turn(_Agent(), f"问题{i}")
        assert spawned == []
        # 第 10 轮：达阈值触发一次
        await mw._record_turn(_Agent(), "问题9")
        await asyncio.gather(*list(middleware_mod._pending_extract))
        assert len(spawned) == 1
        # 提取失败未清零 → 第 11 轮（turns=11 >= 10）立即再触发
        await mw._record_turn(_Agent(), "问题10")
        await asyncio.gather(*list(middleware_mod._pending_extract))
        assert len(spawned) == 2

    _run(scenario())


# ---------------------------------------------------------------------------
# is_locked 预检：已锁不 spawn
# ---------------------------------------------------------------------------


def test_no_spawn_when_locked(make_storage, monkeypatch):
    """锁被占（提取进行中）→ 本轮放弃不 spawn；下轮必然再触发。"""
    spawned: list[dict] = []

    async def _fake_extract(*args, **kwargs):
        spawned.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr(
        middleware_mod, "confirm_persisted_then_run", _fake_extract
    )

    async def scenario():
        from bocomadp.memory.extractor import _lock_key

        storage = make_storage()
        await storage.ensure_ready()
        bus = _FakeBus()
        key = _lock_key("u1", "a1", "s1")
        async with bus.acquire_lock(key):
            mw = _mw(
                MemoryAgentConfigRecord(
                    agent_id="a1", extraction_enabled=True, update_rounds=1
                ),
                memory_storage=storage,
                storage=_FakeFwStorage(),
                message_bus=bus,
            )
            await mw._record_turn(_Agent(), "问题")
        assert spawned == []
        # 锁释放后下一轮恢复触发
        await mw._record_turn(_Agent(), "问题2")
        await asyncio.gather(*list(middleware_mod._pending_extract))
        assert len(spawned) == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# 续轮只 touch 不计
# ---------------------------------------------------------------------------


def test_continuation_touch_only(make_storage):
    """空 query（工具确认/外部结果/resume 续轮）只刷心跳，不计轮不触发。"""
    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1", extraction_enabled=True, update_rounds=2
            ),
            memory_storage=storage,
            storage=_FakeFwStorage(),
            message_bus=_FakeBus(),
        )
        for _ in range(3):
            await mw._record_turn(_Agent(), "")
        # 新语义：非用户轮不建档（touch 只更新），状态保持不存在
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is None
        # 用户输入轮才建档 + 计数
        await mw._record_turn(_Agent(), "问题1")
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is not None
        assert state.turns == 1

    _run(scenario())


def test_injection_only_agent_does_not_count_turns(make_storage, monkeypatch):
    """只开注入（extraction_enabled=False）的 agent：用户轮只刷心跳、不计轮。

    ``turns`` 是提取用的计数。若对不提取的 agent 也累加，这些会话会永久
    满足 ``turns >= 1`` 却永远不会被提取 —— 加 ``limit`` 之后它们会占掉
    提取候选名额（D9 / §3.7-2）。
    """
    calls: list = []

    async def _fake_extract(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(
        middleware_mod, "confirm_persisted_then_run", _fake_extract
    )

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1",
                injection_enabled=True,
                extraction_enabled=False,
                update_rounds=1,
            ),
            memory_storage=storage,
            storage=_FakeFwStorage(),
            message_bus=_FakeBus(),
        )
        for _ in range(3):
            await mw._record_turn(_Agent(), "问题")
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is not None        # 仍建档（心跳链路语义不变）
        assert state.turns == 0         # 但不计数
        assert calls == []              # 也不触发提取

    _run(scenario())


# ---------------------------------------------------------------------------
# create_task 后台化 + _pending_extract 登记/摘除（吸收 background 测试）
# ---------------------------------------------------------------------------


def test_spawn_runs_in_background_and_cleans_pending(make_storage, monkeypatch):
    """触发即返回（不等提取完成）；任务登记 _pending_extract，done 回调摘除；
    reply_id / 会话三元组透传给提取入口。"""
    calls: list[dict] = []

    async def _slow_extract(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        await asyncio.sleep(0.5)  # 模拟慢提取
        return True

    monkeypatch.setattr(
        middleware_mod, "confirm_persisted_then_run", _slow_extract
    )

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1", extraction_enabled=True, update_rounds=1
            ),
            memory_storage=storage,
            storage=_FakeFwStorage(),
            message_bus=_FakeBus(),
        )
        started = time.monotonic()
        await mw._record_turn(_Agent(reply_id="r-42"), "问题")
        # 后台化：对话回合不等待提取（0.5s）完成；阈值需大于一次 DB 往返
        # （真实 MySQL 下 _record_turn 自身有读写），远小于提取耗时。
        assert time.monotonic() - started < 0.2
        tasks = list(middleware_mod._pending_extract)
        assert len(tasks) == 1  # 已登记
        await asyncio.gather(*tasks)
        assert len(calls) == 1
        assert not middleware_mod._pending_extract  # done 回调已摘除
        # 参数透传：会话三元组 + reply_id
        args, kwargs = calls[0]["args"], calls[0]["kwargs"]
        assert args[3:6] == ("u1", "a1", "s1")
        assert kwargs["reply_id"] == "r-42"

    _run(scenario())


# ---------------------------------------------------------------------------
# 检索默认路径：mapping 构造请求 → platform.search_memory
# ---------------------------------------------------------------------------


def test_retrieve_builds_request_via_mapping(monkeypatch):
    """默认检索：build_search_request(cfg, user_id, query) 构造请求对象，
    platform.search_memory 收到的就是它（单一真源）。"""
    captured: dict = {}
    items = [{"memory": "用户喜欢简洁回答"}, {"memory": "常驻上海"}]

    async def _fake_search(request):
        captured["req"] = request
        return items

    monkeypatch.setattr(pf, "search_memory", _fake_search)

    mw = _mw(
        MemoryAgentConfigRecord(
            agent_id="a1", injection_enabled=True, caller="c1", top_k=7
        ),
    )
    texts = _run(mw._retrieve("查找偏好"))
    req = captured["req"]
    assert req.keyword == "查找偏好"
    assert req.user_code == "u1"
    assert req.agent_id == "a1"
    assert req.top_k == 7
    assert texts == ["用户喜欢简洁回答", "常驻上海"]


def test_on_reply_framework_inputs_shape(make_storage, monkeypatch):
    """框架真实入参形态 {"inputs": UserMsg}：检索发起 + 建档计数 + 触发
    （§4 前置缺陷回归——历史版本读 "msg" 键恒空导致全链路失效）。"""
    captured: dict = {}

    async def _fake_search(request):
        captured["search"] = request
        return [{"memory": "用户偏好简洁"}]

    async def _fake_extract(*args, **kwargs):
        captured["extract"] = kwargs
        return True

    monkeypatch.setattr(pf, "search_memory", _fake_search)
    monkeypatch.setattr(
        middleware_mod, "confirm_persisted_then_run", _fake_extract
    )

    async def _handler(**kwargs):
        if False:
            yield None

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        user_msg = UserMsg(name="user", content="你好")
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1",
                injection_enabled=True,
                extraction_enabled=True,
                update_rounds=1,
                caller="c1",
            ),
            memory_storage=storage,
            storage=_FakeFwStorage([user_msg]),
            message_bus=_FakeBus(),
        )
        async for _ in mw.on_reply(
            _Agent(reply_id="r-1"),
            {"inputs": user_msg},
            _handler,
        ):
            pass
        await asyncio.gather(*list(middleware_mod._pending_extract))
        assert "search" in captured              # 检索发起（修复前恒不发起）
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is not None and state.turns == 1    # 计数恢复
        # 首条 user 无前一条 → 空游标（首次提取走最近 50，全为开启期内容）
        assert state.cursor_msg_id == ""
        assert captured["extract"]["reply_id"] == "r-1"

    _run(scenario())


def test_epoch_reset_at_first_user_turn_keeps_all_new_turns(make_storage):
    """epoch 落后 → 首个用户轮 U1 就地重置（游标 = 开关边界）；锁内读窗
    覆盖 U1..Uk 全量（§9.5：重置不吞中间轮次）。"""
    from bocomadp.memory.extractor import _read_window

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        # 框架消息：关闭期 1 轮 + 重开后 5 轮
        msgs = [
            _msg("user", "关闭前问题", 1),
            _msg("assistant", "关闭前回答", 2),
        ]
        for i in range(5):
            msgs.append(_msg("user", f"U{i}", 3 + i * 2))
            msgs.append(_msg("assistant", f"A{i}", 4 + i * 2))
        framework = _FakeFwStorage(msgs)
        mw = _mw(
            MemoryAgentConfigRecord(
                agent_id="a1",
                extraction_enabled=True,
                switch_epoch=1,
                update_rounds=5,
            ),
            memory_storage=storage,
            storage=framework,
            message_bus=_FakeBus(),
        )
        # 预置旧 epoch 状态（游标空 → 兜底对照）
        await storage.ensure_memory_session_state(
            "u1", "a1", "s1",
            cursor_created_at="", cursor_msg_id="", epoch=0, now=1000.0,
        )
        # U1..U5：U1 触发 middleware 重置，U5 达阈值
        for i in range(5):
            await mw._record_turn(_Agent(), f"U{i}", msgs[2 + i * 2].id)
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state.switch_epoch == 1           # U1 已收敛
        # 游标 = U1 的前一条（关闭期 assistant）→ 窗口 = U1..U5 全量
        window = await _read_window(
            framework, "u1", "s1",
            (state.cursor_created_at, state.cursor_msg_id),
        )
        texts = [m.get_text_content() for m in window]
        assert "关闭前问题" not in texts and "关闭前回答" not in texts
        assert [t for t in texts if t.startswith("U")] == [
            f"U{i}" for i in range(5)
        ]

    _run(scenario())


def test_touch_none_skips_increment(make_storage, monkeypatch):
    """touch 返回 None（状态恰被 TTL 清理）→ 跳过 increment，
    不造"只有 turns、无游标"的状态（§14.9 / §9.15）。"""
    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        mw = _mw(
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True),
            memory_storage=storage,
            storage=_FakeFwStorage(),
            message_bus=_FakeBus(),
        )
        await mw._record_turn(_Agent(), "问题")
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is not None and state.turns == 1

        async def _none_touch(*args, **kwargs):
            return None

        monkeypatch.setattr(storage, "touch_memory_session", _none_touch)
        await mw._record_turn(_Agent(), "问题2")  # touch None → 本轮放弃
        state = await storage.get_memory_session_state("u1", "a1", "s1")
        assert state is not None and state.turns == 1  # 未 +1

    _run(scenario())


def test_can_retrieve_requires_caller():
    """无 caller（注册未完成的 pending 窗口期）→ 纯透传计数，不发起平台检索。"""
    assert _mw(
        MemoryAgentConfigRecord(agent_id="a1", injection_enabled=True)
    )._can_retrieve() is False
    assert _mw(
        MemoryAgentConfigRecord(agent_id="a1", injection_enabled=True, caller="c1")
    )._can_retrieve() is True
    # 注入开关关闭时即使已有 caller 也不检索
    assert _mw(
        MemoryAgentConfigRecord(agent_id="a1", injection_enabled=False, caller="c1")
    )._can_retrieve() is False


# ---------------------------------------------------------------------------
# 注入行为（自旧 test_memory_middleware.py 保留）
# ---------------------------------------------------------------------------


def test_inject_appends_retains_old_memory():
    """命中注入：追加新记忆消息，历史记忆消息保留（仿框架 mem0）。"""
    mw = _mw(MemoryAgentConfigRecord(agent_id="a1"))
    agent = _Agent()
    agent.state.context.append(_msg("assistant", "旧记忆", 1))
    mw._inject_memories(agent, ["记忆A", "记忆B"])
    assert len(agent.state.context) == 2
    injected = agent.state.context[-1]
    assert injected.role == "assistant"
    assert injected.name == middleware_mod.MEMORY_MESSAGE_NAME
    assert len(injected.content) == 1
    assert isinstance(injected.content[0], HintBlock)
    assert injected.content[0].hint == "记忆A\n记忆B"


def test_inject_lands_right_after_user_message():
    """落点：注入消息紧随本轮用户消息之后（ReplyStart 时机保证）。"""
    mw = _mw(MemoryAgentConfigRecord(agent_id="a1"))
    agent = _Agent()
    user_msg = _msg("user", "我之前说过什么？", 1)
    agent.state.context.append(user_msg)
    mw._inject_memories(agent, ["记忆A"])
    assert agent.state.context[0] is user_msg
    assert agent.state.context[1].name == middleware_mod.MEMORY_MESSAGE_NAME


def test_inject_empty_keeps_existing_memory():
    """空命中：不注入新消息，已有记忆历史原样保留。"""
    mw = _mw(MemoryAgentConfigRecord(agent_id="a1"))
    agent = _Agent()
    old = _msg("assistant", "旧记忆", 1)
    agent.state.context.append(old)
    mw._inject_memories(agent, [])
    assert agent.state.context == [old]


def test_item_to_text_variants():
    """平台返回项归一化：dict 取 memory 字段，其余原样 str。"""
    assert (
        MemoryMiddleware._item_to_text({"memory": "文本"})
        == "文本"
    )
    assert MemoryMiddleware._item_to_text("裸字符串") == "裸字符串"
    assert MemoryMiddleware._item_to_text({"other": 1}) == "{'other': 1}"


def test_on_reasoning_touches_and_forwards_input_kwargs():
    """回归：心跳钩子删参后再透传会抛 UnboundLocalError。"""
    touched: list = []
    calls: list = []

    async def _touch(user_id, agent_id, session_id, now=None):
        touched.append((user_id, agent_id, session_id))

    async def _events():
        yield "evt"

    def handler(**kwargs):
        calls.append(kwargs)
        return _events()

    mw = _mw(
        MemoryAgentConfigRecord(agent_id="a1"),
        memory_storage=SimpleNamespace(touch_memory_session=_touch),
    )

    async def _scenario():
        return [e async for e in mw.on_reasoning(None, {"inputs": "q"}, handler)]

    assert _run(_scenario()) == ["evt"]
    assert calls == [{"inputs": "q"}]
    assert touched == [("u1", "a1", "s1")]
