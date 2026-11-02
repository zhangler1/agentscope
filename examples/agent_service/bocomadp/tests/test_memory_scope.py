# -*- coding: utf-8 -*-
"""memory/sweeper.py 单测（Task 12，§7.4）：静默扫描 + 真实 owner + 超龄清理。

形态：同步测试函数 + ``asyncio.run(scenario())`` 收口；候选/清理走
make_storage 双后端工厂（协程内 ensure_ready）；agent 配置 monkeypatch
``config_store.list_extraction_enabled``、平台保存 monkeypatch
``platform.save_memories``——扫描→提取全链路为真（锁内五步、游标推进、
turns 清零），仅平台边界为 fake。

- 共享 agent 多 owner：候选来自会话状态表（非配置归属者枚举），
  u1/u2 都被提取且平台 userCode 是真实 owner；
- 超龄清理：last_active 距今超过保留窗口 → delete_memory_session_state
  且不提取（遗忘后重新开始）；
- 候选只含静默会话：活跃会话（last_active_before 范围下推）不被触碰。
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from agentscope.message import AssistantMsg, UserMsg

from bocomadp.memory import config_store as memory_store
from bocomadp.memory import platform as pf
from bocomadp.memory import sweeper as sweeper_mod
from bocomadp.memory.config import MemoryGlobalConfig
from bocomadp.memory.storage._models import MemoryAgentConfigRecord
from bocomadp.memory.settings import MemorySettings, configure_settings, reset_settings
from bocomadp.memory.sweeper import MemorySweeper


@pytest.fixture(autouse=True)
def _deploy_settings():
    """注入部署配置：sweeper 提取路径会读 dangling_wait_secs / lock_ttl_secs。"""
    configure_settings(MemorySettings(environment="sit"))
    yield
    reset_settings()


def _run(coro):
    return asyncio.run(coro)


def _msg(role: str, text: str, seq: int):
    factory = UserMsg if role == "user" else AssistantMsg
    return factory(
        name=role,
        content=text,
        created_at=f"2026-09-30T10:00:00.{seq:06d}",
        id=f"m{seq:04d}",
    )


class _FakeBus:
    """假 message_bus：is_locked / acquire_lock 以 asyncio.Lock 实现。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def is_locked(self, key: str) -> bool:
        del key
        return self._lock.locked()

    def acquire_lock(self, key: str, ttl_secs: int = 600):
        del key, ttl_secs

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _inner():
            async with self._lock:
                yield

        return _inner()


class _FakeFramework:
    """假框架 storage：按 session_id 提供消息，记录读窗调用。"""

    def __init__(self, messages: dict[str, list]) -> None:
        self.messages = messages
        self.read_calls: list[tuple[str, str]] = []

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        self.read_calls.append((user_id, session_id))
        del user_id
        msgs = sorted(
            self.messages.get(session_id, []),
            key=lambda m: (m.created_at, m.id),
        )
        if before is not None:
            index = next(
                (i for i, m in enumerate(msgs) if m.id == before), None
            )
            if index is None:
                return [], False
            msgs = msgs[:index]
        page = msgs[-limit:]
        return page, len(msgs) > limit


def _install_platform(monkeypatch) -> list:
    """monkeypatch 平台保存与配置清单；返回 save 调用记录列表。"""
    saved: list = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def _list_enabled():
        return [
            (
                "shared",
                MemoryAgentConfigRecord(
                    agent_id="shared", extraction_enabled=True
                ),
            )
        ]

    monkeypatch.setattr(
        memory_store, "list_extraction_enabled", _list_enabled
    )
    return saved


async def _seed_session(
    storage, user_id: str, agent_id: str, session_id: str, *, turns: int = 1
) -> None:
    """预置静默会话：ensure 建档 → 计数 → touch 回静默时刻（已静默 1h）。"""
    silent_at = time.time() - 3600
    await storage.ensure_memory_session_state(
        user_id, agent_id, session_id,
        cursor_created_at="", cursor_msg_id="", epoch=0, now=silent_at,
    )
    for _ in range(turns):
        await storage.increment_memory_session_turns(
            user_id, agent_id, session_id
        )
    await storage.touch_memory_session(
        user_id, agent_id, session_id, now=silent_at
    )


async def _install_config(storage) -> None:
    """把 agent 配置写入存储（锁内兜底 get_memory_config 的数据源）。"""
    await storage.upsert_memory_config(
        MemoryAgentConfigRecord(agent_id="shared", extraction_enabled=True)
    )


# ---------------------------------------------------------------------------
# 共享 agent：候选来自会话状态表，owner 是真实用户
# ---------------------------------------------------------------------------


def test_sweeper_extracts_shared_agent_sessions_with_real_owner(
    make_storage, monkeypatch
):
    """共享 agent 被 u1/u2 分别使用：两会话都提取，平台 userCode 各归其主。"""
    saved = _install_platform(monkeypatch)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _install_config(storage)
        framework = _FakeFramework(
            {
                "s1": [_msg("user", "u1 的问题", 1), _msg("assistant", "u1 的回答", 2)],
                "s2": [_msg("user", "u2 的问题", 1), _msg("assistant", "u2 的回答", 2)],
            }
        )
        await _seed_session(storage, "u1", "shared", "s1")
        await _seed_session(storage, "u2", "shared", "s2")

        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        attempts = await sweeper._sweep_once(MemoryGlobalConfig(idle_minutes=15))
        assert attempts == 2
        # 两个会话都完成提取：保存两次、userCode 是真实 owner（非配置归属者）
        assert len(saved) == 2
        assert {req.user_code for req in saved} == {"u1", "u2"}
        assert {req.agent_id for req in saved} == {"shared"}
        # 读窗按 (user_id, session_id) 各自进行
        assert ("u1", "s1") in framework.read_calls
        assert ("u2", "s2") in framework.read_calls
        # 全链路收尾：游标推进 + turns 清零
        for user_id, session_id in (("u1", "s1"), ("u2", "s2")):
            state = await storage.get_memory_session_state(
                user_id, "shared", session_id
            )
            assert state is not None
            assert state.turns == 0
            assert state.cursor_msg_id == "m0002"

    _run(scenario())


# ---------------------------------------------------------------------------
# 超龄清理：delete_memory_session_state 且不提取
# ---------------------------------------------------------------------------


def test_sweeper_purges_stale_session_state(make_storage, monkeypatch):
    """最后活跃超过保留窗口 → 会话状态被删除且不提取（遗忘后重新开始）。"""
    saved = _install_platform(monkeypatch)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        framework = _FakeFramework({"s1": [_msg("user", "旧问题", 1)]})
        await _install_config(storage)
        # 建档 + 计数后 touch 8 天前（> state_ttl_days=7 → 超龄）
        stale_at = time.time() - 8 * 86400
        await storage.ensure_memory_session_state(
            "u1", "shared", "s1",
            cursor_created_at="", cursor_msg_id="", epoch=0, now=stale_at,
        )
        await storage.increment_memory_session_turns("u1", "shared", "s1")
        await storage.touch_memory_session("u1", "shared", "s1", now=stale_at)

        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        attempts = await sweeper._sweep_once(
            MemoryGlobalConfig(idle_minutes=15, state_ttl_days=7)
        )
        assert attempts == 0  # 超龄跳过提取
        assert saved == []    # 平台保存未发生
        assert framework.read_calls == []  # 连读窗都没发生
        # 会话运行时状态已被删除（下次对话从 1 重新计数、重建游标）
        state = await storage.get_memory_session_state("u1", "shared", "s1")
        assert state is None

    _run(scenario())


# ---------------------------------------------------------------------------
# 候选只含静默会话：活跃会话不被触碰
# ---------------------------------------------------------------------------


def test_sweeper_only_takes_idle_sessions(make_storage, monkeypatch):
    """范围下推：last_active_before 之外的活跃会话完全不进入处理。"""
    saved = _install_platform(monkeypatch)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _install_config(storage)
        framework = _FakeFramework(
            {
                "s1": [_msg("user", "活跃会话的问题", 1)],
                "s2": [_msg("user", "静默会话的问题", 1), _msg("assistant", "回答", 2)],
            }
        )
        # s1 活跃（建档 + 计数 + 刚刚 touch）；s2 静默 1h
        await storage.ensure_memory_session_state(
            "u1", "shared", "s1",
            cursor_created_at="", cursor_msg_id="", epoch=0,
            now=time.time(),
        )
        await storage.increment_memory_session_turns("u1", "shared", "s1")
        await storage.touch_memory_session(
            "u1", "shared", "s1", now=time.time()
        )
        await _seed_session(storage, "u2", "shared", "s2")

        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        attempts = await sweeper._sweep_once(MemoryGlobalConfig(idle_minutes=15))
        assert attempts == 1  # 只处理静默会话 s2
        assert framework.read_calls == [("u2", "s2")]  # s1 从未被读窗
        assert len(saved) == 1
        assert saved[0].user_code == "u2"
        # 活跃会话状态原样保留
        state = await storage.get_memory_session_state("u1", "shared", "s1")
        assert state is not None and state.turns == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# 未开启记忆的 agent：候选命中但配置缺失 → 跳过
# ---------------------------------------------------------------------------


def test_sweeper_epoch_stale_resets_and_skips(make_storage, monkeypatch):
    """epoch 落后 + 静默路径 → 锁内兜底重置（对齐最新）→ 窗口空 skip；
    epoch 同步、游标推进到最新（§6.5 场景 1 / §9.6）。"""
    saved = _install_platform(monkeypatch)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        # 全局配置 epoch=1（模拟重开），会话状态 epoch=0（落后）
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(
                agent_id="shared", extraction_enabled=True, switch_epoch=1
            )
        )
        framework = _FakeFramework(
            {"s1": [_msg("user", "关闭期问题", 1), _msg("assistant", "关闭期回答", 2)]}
        )
        await _seed_session(storage, "u1", "shared", "s1")

        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        attempts = await sweeper._sweep_once(MemoryGlobalConfig(idle_minutes=15))
        assert attempts == 1     # 发起（锁内兜底重置）
        assert saved == []       # 重置后窗口空 → 不提取
        state = await storage.get_memory_session_state("u1", "shared", "s1")
        assert state is not None
        assert state.switch_epoch == 1       # epoch 已同步
        assert state.cursor_msg_id == "m0002"  # 游标对齐最新
        assert state.turns == 0

    _run(scenario())


def test_sweeper_skips_agent_without_extraction_enabled(
    make_storage, monkeypatch
):
    """会话存在但 agent 未开启记忆（清单缺失）→ 不提取。"""
    saved: list = []

    async def _save(request):
        saved.append(request)

    monkeypatch.setattr(pf, "save_memories", _save)

    async def _list_enabled():
        return []  # 无任何开启提取的 agent

    monkeypatch.setattr(
        memory_store, "list_extraction_enabled", _list_enabled
    )

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        framework = _FakeFramework(
            {"s1": [_msg("user", "问题", 1), _msg("assistant", "回答", 2)]}
        )
        await _seed_session(storage, "u1", "shared", "s1")

        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        attempts = await sweeper._sweep_once(MemoryGlobalConfig(idle_minutes=15))
        assert attempts == 0
        assert saved == []
        assert framework.read_calls == []

    _run(scenario())


# ---------------------------------------------------------------------------
# 批量清理（§2.5/§2.6）：多层限流、瞬态错误容错、积压告警
# ---------------------------------------------------------------------------


async def _seed_stale(storage, count: int, *, aged_secs: float = 8 * 86400):
    """预置 ``count`` 条超龄会话状态（心跳在保留窗口之外）。"""
    stale_at = time.time() - aged_secs
    for i in range(count):
        await storage.ensure_memory_session_state(
            f"u{i}", "shared", f"s{i}",
            cursor_created_at="", cursor_msg_id="", epoch=0, now=stale_at + i,
        )


def test_sweeper_purge_loops_multiple_batches(
    make_storage, session_ids, monkeypatch
):
    """单批装不下时循环取下一批：一轮内清空（`_PURGE_MAX_BATCHES` 内）。"""
    monkeypatch.setattr(sweeper_mod, "_PURGE_BATCH", 2)
    monkeypatch.setattr(sweeper_mod, "_PURGE_MAX_BATCHES", 10)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _seed_stale(storage, 5)
        sweeper = MemorySweeper(_FakeBus(), storage, _FakeFramework({}))
        total = await sweeper._purge_stale(
            MemoryGlobalConfig(idle_minutes=15), time.time()
        )
        assert total == 5
        assert await session_ids(storage) == []

    _run(scenario())


def test_sweeper_purge_capped_by_max_batches(
    make_storage, session_ids, monkeypatch
):
    """单轮批数封顶：超出上限的积压留到下一轮（单轮有界）。"""
    monkeypatch.setattr(sweeper_mod, "_PURGE_BATCH", 2)
    monkeypatch.setattr(sweeper_mod, "_PURGE_MAX_BATCHES", 2)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _seed_stale(storage, 10)
        sweeper = MemorySweeper(_FakeBus(), storage, _FakeFramework({}))
        total = await sweeper._purge_stale(
            MemoryGlobalConfig(idle_minutes=15), time.time()
        )
        assert total == 4  # 2 批 × 2 条
        assert len(await session_ids(storage)) == 6

    _run(scenario())


def test_sweeper_purge_warns_on_backlog(
    make_storage, monkeypatch, caplog
):
    """达单轮上限 → warning（积压可观测，替代配置旋钮）。"""
    monkeypatch.setattr(sweeper_mod, "_PURGE_BATCH", 1)
    monkeypatch.setattr(sweeper_mod, "_PURGE_MAX_BATCHES", 1)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _seed_stale(storage, 3)
        sweeper = MemorySweeper(_FakeBus(), storage, _FakeFramework({}))
        with caplog.at_level(logging.WARNING):
            total = await sweeper._purge_stale(
                MemoryGlobalConfig(idle_minutes=15), time.time()
            )
        assert total == 1
        assert "purge backlog" in caplog.text

    _run(scenario())


def test_sweeper_purge_failure_does_not_block_extraction(
    make_storage, monkeypatch, caplog
):
    """清理抛瞬态错误（1213 死锁 / 1205 锁超时）→ 不冒泡，本轮仍做提取。"""
    saved = _install_platform(monkeypatch)

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()
        await _install_config(storage)
        await _seed_session(storage, "u1", "shared", "s1")
        framework = _FakeFramework(
            {"s1": [_msg("user", "问题", 1), _msg("assistant", "回答", 2)]}
        )

        async def _boom(*args, **kwargs):
            raise OperationalError("DELETE", {}, Exception("1213 deadlock"))

        storage.delete_stale_session_states = _boom
        sweeper = MemorySweeper(_FakeBus(), storage, framework)
        with caplog.at_level(logging.WARNING):
            attempts = await sweeper._sweep_once(
                MemoryGlobalConfig(idle_minutes=15)
            )
        assert attempts == 1        # 提取照常发生
        assert len(saved) == 1
        assert "purge batch failed" in caplog.text

    _run(scenario())


def test_sweeper_purge_propagates_cancelled(make_storage):
    """`CancelledError` 必须原样上抛（shutdown 时要能取消 sweeper）。"""

    async def scenario():
        storage = make_storage()
        await storage.ensure_ready()

        async def _cancel(*args, **kwargs):
            raise asyncio.CancelledError()

        storage.delete_stale_session_states = _cancel
        sweeper = MemorySweeper(_FakeBus(), storage, _FakeFramework({}))
        await sweeper._purge_stale(
            MemoryGlobalConfig(idle_minutes=15), time.time()
        )

    with pytest.raises(asyncio.CancelledError):
        _run(scenario())
