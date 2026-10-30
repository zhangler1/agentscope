# -*- coding: utf-8 -*-
"""/chat 并发闸机三维占位/回滚/释放测试。

覆盖两套实现（同接口鸭子类型）：

1. MemoryGuard（本地全路径）：
   - agent 上限内放行 / 达上限拒绝 reason=agent / 未配置不限；
   - base_dims=False（平台总开关关闭、仅智能体配置激活）时用户/全局
     两维跳过不占位，agent 维度照常计数；
   - 超限回滚不泄漏（低层被拒不留下已占名额）；
   - 并发占位：asyncio.gather 压并发，成功数恰等于上限；
   - 锁观察释放：run 锁出现→消失自动释放；锁永不出现超时兜底释放；
   - rollback 释放名额。
2. ConcurrencyGuard（Redis 版，FakeRedis）：
   - agent 维度占位/拒绝/回滚；
   - register 写伴随哈希；对账清理同步 DECR agent 计数 + HDEL 伴随哈希。
3. 释放时机（run 结束即释放）：
   - run 结束后 release_session 立刻退名额（不等锁观察/对账）；
   - 释放早于登记（run 秒失败）时，登记阶段退掉占位、不再登记；
   - run_release 钩子：run 正常返回或抛异常都触发释放。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio

from bocomadp.concurrency.guard import ConcurrencyGuard
from bocomadp.concurrency.memory_guard import MemoryGuard


def _memory_guard(
    *,
    max_running: int = 0,
    max_running_per_user: int = 0,
    limits: dict[str, int] | None = None,
    lock_checker=None,
) -> MemoryGuard:
    """构造内存闸机；limits 即 agent_limit_provider 的数据源（未配置=不限）。"""
    table = limits or {}

    async def provider(agent_id: str):
        return table.get(agent_id)

    return MemoryGuard(
        max_running=max_running,
        max_running_per_user=max_running_per_user,
        agent_limit_provider=provider,
        lock_checker=lock_checker,
        watch_interval=0.01,
        watch_timeout=0.3,
    )


# ── MemoryGuard：占位与拒绝 ──────────────────────────────────────


def test_agent_limit_blocks_and_releases():
    async def _scenario() -> None:
        g = _memory_guard(limits={"a1": 1})
        assert await g.try_acquire_ex("u1", "a1") is None
        reason = await g.try_acquire_ex("u2", "a1")
        assert reason == "agent"
        # 低层(用户)计数已回滚,不泄漏
        assert g._user.get("u2") is None
        # 前一个 run 释放后恢复放行
        await g.rollback("", "u1", "a1")
        assert await g.try_acquire_ex("u2", "a1") is None

    asyncio.run(_scenario())


def test_unconfigured_agent_is_unlimited():
    async def _scenario() -> None:
        g = _memory_guard(limits={})
        for i in range(5):
            assert await g.try_acquire_ex(f"u{i}", "aX") is None
        # 未配置 = 维度跳过不占位(零开销),计数不存在
        assert g._agent.get("aX") is None

    asyncio.run(_scenario())


def test_base_dims_skipped_when_disabled():
    async def _scenario() -> None:
        # 用户/全局上限都设了,但 base_dims=False 时必须跳过不占位
        g = _memory_guard(max_running=1, max_running_per_user=1, limits={"a1": 10})
        assert await g.try_acquire_ex("u1", "a1", base_dims=False) is None
        assert await g.try_acquire_ex("u1", "a1", base_dims=False) is None
        assert g._global == 0
        assert g._user.get("u1") is None
        # agent 维度照常计数
        assert g._agent.get("a1") == 2

    asyncio.run(_scenario())


def test_global_rejection_rolls_back_agent_counter():
    async def _scenario() -> None:
        g = _memory_guard(max_running=1, limits={"a1": 5})
        assert await g.try_acquire_ex("u1", "a1") is None
        reason = await g.try_acquire_ex("u2", "a1")
        assert reason == "global"
        # agent 计数已回滚,只剩 u1 占的 1 个
        assert g._agent.get("a1") == 1

    asyncio.run(_scenario())


def test_concurrent_acquire_respects_agent_limit():
    async def _scenario() -> None:
        g = _memory_guard(limits={"a1": 3})
        results = await asyncio.gather(
            *[g.try_acquire_ex(f"u{i}", "a1") for i in range(10)],
        )
        ok = [r for r in results if r is None]
        blocked = [r for r in results if r == "agent"]
        assert len(ok) == 3
        assert len(blocked) == 7
        assert g._agent["a1"] == 3

    asyncio.run(_scenario())


def test_provider_failure_fails_open():
    async def _scenario() -> None:
        async def broken_provider(agent_id: str):
            raise RuntimeError("config layer down")

        g = MemoryGuard(
            max_running=0,
            max_running_per_user=0,
            agent_limit_provider=broken_provider,
        )
        # 配置查询失败 → 视为未配置,fail-open 放行(维度跳过不占位)
        assert await g.try_acquire_ex("u1", "a1") is None
        assert g._agent.get("a1") is None

    asyncio.run(_scenario())


# ── MemoryGuard：锁观察释放 ──────────────────────────────────────


def test_watch_releases_after_lock_gone():
    async def _scenario() -> None:
        lock_state = {"locked": False}

        async def checker(session_id: str) -> bool:
            return lock_state["locked"]

        g = _memory_guard(limits={"a1": 1}, lock_checker=checker)
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.register("s1", "u1", "a1")

        # run 跑起来(锁出现)
        lock_state["locked"] = True
        await asyncio.sleep(0.05)
        assert "s1" in g._registry

        # run 结束(锁消失)→ 观察任务自动释放名额
        lock_state["locked"] = False
        for _ in range(50):
            if "s1" not in g._registry:
                break
            await asyncio.sleep(0.02)
        assert "s1" not in g._registry
        assert g._global == 0
        assert g._user.get("u1") is None
        assert g._agent.get("a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_watch_timeout_releases_when_lock_never_appears():
    async def _scenario() -> None:
        async def checker(session_id: str) -> bool:
            return False  # 锁永不出现(装配失败场景)

        g = _memory_guard(limits={"a1": 1}, lock_checker=checker)
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.register("s1", "u1", "a1")
        # watch_timeout=0.3 → 兜底释放,名额不泄漏
        await asyncio.sleep(0.5)
        assert "s1" not in g._registry
        assert g._agent.get("a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_rollback_releases_all_dimensions():
    async def _scenario() -> None:
        g = _memory_guard(max_running=5, limits={"a1": 2})
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.rollback("", "u1", "a1")
        assert g._global == 0
        assert g._user.get("u1") is None
        assert g._agent.get("a1") is None

    asyncio.run(_scenario())


# ── ConcurrencyGuard（Redis 版，FakeRedis） ──────────────────────


def test_redis_agent_dim_acquire_and_block(fake_redis):
    async def _scenario() -> None:
        async def provider(agent_id: str):
            return {"a1": 1}.get(agent_id)

        g = ConcurrencyGuard(
            lambda: fake_redis,
            max_running=0,
            max_running_per_user=0,
            agent_limit_provider=provider,
        )
        assert await g.try_acquire_ex("u1", "a1") is None
        assert await fake_redis.get("agentscope:running:agent:a1") == "1"
        assert await g.try_acquire_ex("u2", "a1") == "agent"
        # 低层(用户)计数已回滚
        assert await fake_redis.get("agentscope:running:user:u2") is None
        # 释放后恢复
        await g.rollback("", "u1", "a1")
        assert await g.try_acquire_ex("u2", "a1") is None

    asyncio.run(_scenario())


def test_redis_register_writes_session_agent(fake_redis):
    async def _scenario() -> None:
        g = ConcurrencyGuard(lambda: fake_redis, max_running=0, max_running_per_user=0)
        await g.try_acquire_ex("u1", "a1")
        await g.register("s1", "u1", "a1")
        assert (
            await fake_redis.hget("agentscope:running:session_agent", "s1") == "a1"
        )
        await g.close()

    asyncio.run(_scenario())


def test_redis_reconcile_releases_agent_counter(fake_redis):
    async def _scenario() -> None:
        async def provider(agent_id: str):
            return {"a1": 5}.get(agent_id)

        g = ConcurrencyGuard(
            lambda: fake_redis,
            max_running=0,
            max_running_per_user=0,
            agent_limit_provider=provider,
        )
        await g.try_acquire_ex("u1", "a1")
        await g.register("s1", "u1", "a1")
        # 锁从未出现 + grace=0 → 对账立即清理,agent 计数与伴随哈希同步释放
        cleaned = await g.reconcile(0)
        assert cleaned == 1
        assert await fake_redis.get("agentscope:running:agent:a1") in (None, "0")
        assert (
            await fake_redis.hget("agentscope:running:session_agent", "s1") is None
        )
        await g.close()

    asyncio.run(_scenario())


# ── 释放时机：run 结束即释放 ──────────────────────────────────────


def test_memory_release_session_frees_slot():
    """run 结束 → 立刻退名额（不等锁观察/对账兜底）。"""

    async def _scenario() -> None:
        g = _memory_guard(limits={"a1": 1})
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.reserve("s1")
        await g.register("s1", "u1", "a1")

        assert await g.release_session("s1") is True
        assert g._agent.get("a1") is None
        assert g._user.get("u1") is None
        # 名额已回 → 另一个会话可发
        assert await g.try_acquire_ex("u2", "a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_memory_release_before_register_returns_slot():
    """run 秒失败：释放先于登记到达 → 登记阶段退掉占位且不登记。"""

    async def _scenario() -> None:
        g = _memory_guard(limits={"a1": 1})
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.reserve("s1")

        # run 已结束（登记还没发生）
        assert await g.release_session("s1") is True
        # 登记随后到达 → 退掉本轮占位，不再登记
        await g.register("s1", "u1", "a1")
        assert g._agent.get("a1") is None
        assert "s1" not in g._registry
        # 名额已回 → 新会话可发
        assert await g.try_acquire_ex("u2", "a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_redis_release_session_frees_slot(fake_redis):
    """Redis 版：run 结束 → 原子释放登记名额与三维计数。"""

    async def _scenario() -> None:
        async def provider(agent_id: str):
            return {"a1": 1}.get(agent_id)

        g = ConcurrencyGuard(
            lambda: fake_redis,
            max_running=0,
            max_running_per_user=0,
            agent_limit_provider=provider,
        )
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.reserve("s1")
        await g.register("s1", "u1", "a1")

        assert await g.release_session("s1") is True
        assert await fake_redis.get("agentscope:running:agent:a1") in (None, "0")
        assert await fake_redis.hget("agentscope:running:sessions", "s1") is None
        assert (
            await fake_redis.hget("agentscope:running:session_agent", "s1") is None
        )
        # 名额已回 → 另一个会话可发
        assert await g.try_acquire_ex("u2", "a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_redis_release_before_register_returns_slot(fake_redis):
    """Redis 版：释放早于登记 → 登记阶段退占位、不写登记。"""

    async def _scenario() -> None:
        async def provider(agent_id: str):
            return {"a1": 1}.get(agent_id)

        g = ConcurrencyGuard(
            lambda: fake_redis,
            max_running=0,
            max_running_per_user=0,
            agent_limit_provider=provider,
        )
        assert await g.try_acquire_ex("u1", "a1") is None
        await g.reserve("s1")

        assert await g.release_session("s1") is True
        await g.register("s1", "u1", "a1")
        assert await fake_redis.get("agentscope:running:agent:a1") in (None, "0")
        assert await fake_redis.hget("agentscope:running:sessions", "s1") is None
        # 名额已回 → 新会话可发
        assert await g.try_acquire_ex("u2", "a1") is None
        await g.close()

    asyncio.run(_scenario())


def test_run_release_hook_releases_when_run_ends():
    """run_release 钩子：run 正常返回或抛异常，都触发该会话释放。"""
    from bocomadp.concurrency.run_release import _wrap_run

    released: list[str] = []

    class _Guard:
        async def release_session(self, session_id: str) -> bool:
            released.append(session_id)
            return True

    class _Svc:
        pass

    async def _ok(self, user_id, session_id, agent_id, input_msg=None, run_id=None):
        return "done"

    async def _boom(self, user_id, session_id, agent_id, input_msg=None, run_id=None):
        raise RuntimeError("run failed")

    async def _scenario() -> None:
        assert await _wrap_run(_ok, _Guard())(_Svc(), "u1", "s1", "a1") == "done"
        assert released == ["s1"]

        released.clear()
        try:
            await _wrap_run(_boom, _Guard())(_Svc(), "u1", "s2", "a1")
        except RuntimeError:
            pass
        assert released == ["s2"]

    asyncio.run(_scenario())
