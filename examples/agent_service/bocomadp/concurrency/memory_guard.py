# -*- coding: utf-8 -*-
"""内存版闸机（InMemory 总线 / 本地模式）的三维占位与释放。

与 Redis 版 ``ConcurrencyGuard`` 同接口（鸭子类型，中间件无感切换）：

- dict 计数器（global / per-user / per-agent 三维）+ 进程内注册表；
- 无需 Lua 与对账：单进程无跨实例漂移，释放挂进程内锁观察任务
  （run 持有会话锁 → 锁消失即 run 结束 → 释放名额），进程重启计数
  天然归零（run 也随之消亡，计数与现实自动一致）；
- 锁始终未出现（装配失败/进程内异常）→ 超时兜底释放，避免名额单向泄漏；
- **主释放信号是"run 结束"**：``run_release`` 模块在 ``ChatService.run``
  的 finally 里调用 ``release_session``，run 一结束就退名额，不依赖锁观察
  的时序（run 秒失败时锁只存在几毫秒，锁观察必然错过）；
- 占位与登记分两步（入口占位、响应后登记），两步之间 run 可能已结束：
  ``reserve`` 记"待转正"，``release_session`` 先到则记提前结束标记，
  ``register`` 见到标记即退掉占位、不再登记；
- 异常路径 fail-open 精神：``agent_limit_provider`` 抛错视为未配置（不限）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("bocomadp.concurrency.memory_guard")

# 提前结束标记的存活时长（秒）：run 已结束、登记还没发生的等待窗口
_EARLY_TTL_SECS = 300.0

# try_acquire_ex 被拒维度（与限流响应 reason 字段同值）
REASON_USER = "user"
REASON_AGENT = "agent"
REASON_GLOBAL = "global"


class MemoryGuard:
    """In-process concurrency limiter for the /chat endpoint."""

    def __init__(
        self,
        *,
        max_running: int = 10,
        max_running_per_user: int = 3,
        agent_limit_provider: Callable[[str], Awaitable[int | None]] | None = None,
        lock_checker: Callable[[str], Awaitable[bool]] | None = None,
        watch_interval: float = 0.2,
        watch_timeout: float = 90.0,
    ) -> None:
        self._max_running = max_running
        self._max_running_per_user = max_running_per_user
        self._agent_limit_provider = agent_limit_provider
        self._lock_checker = lock_checker
        self._watch_interval = watch_interval
        self._watch_timeout = watch_timeout
        # 计数器：global 一个数；user/agent 各一个 dict（按维度键计数）
        self._global = 0
        self._user: dict[str, int] = {}
        self._agent: dict[str, int] = {}
        # 注册表：sid → (user_id, agent_id, 注册时刻)
        self._registry: dict[str, tuple[str, str, float]] = {}
        # 待转正占位：sid → 次数（入口已占位，响应后登记尚未发生）
        self._pending: dict[str, int] = {}
        # 提前结束标记：sid → 时刻（run 已结束，但登记还没发生）
        self._early: dict[str, float] = {}
        self._watch_tasks: set[asyncio.Task] = set()

    # ── 占位 ─────────────────────────────────────────────────────

    async def try_acquire(self, user_id: str) -> bool:
        """兼容 Redis 版的旧签名：仅用户/全局两维，返回是否占位成功。"""
        return await self.try_acquire_ex(user_id) is None

    async def try_acquire_ex(
        self,
        user_id: str,
        agent_id: str = "",
        *,
        base_dims: bool = True,
    ) -> str | None:
        """三维顺序占位：用户 → 智能体 → 全局，超限回滚已占名额。

        成功返回 ``None``；被拒返回维度名（"user" / "agent" / "global"）。
        ``base_dims=False``（平台总开关关闭、闸机仅被智能体配置激活）时
        用户/全局两维跳过不占位；agent 维度未配置（provider 返回 None）
        同样跳过——未配置 = 不限。
        """
        occupied: list[tuple[dict[str, int], str]] = []

        if base_dims and self._max_running_per_user > 0:
            cur = self._user.get(user_id, 0) + 1
            if cur > self._max_running_per_user:
                return REASON_USER
            self._user[user_id] = cur
            occupied.append((self._user, user_id))

        if agent_id and self._agent_limit_provider is not None:
            try:
                limit = await self._agent_limit_provider(agent_id)
            except Exception:  # noqa: BLE001 — fail-open：配置查询失败视为不限
                logger.exception("agent_limit_provider failed; treat as unlimited")
                limit = None
            if limit is not None and limit > 0:
                cur = self._agent.get(agent_id, 0) + 1
                if cur > limit:
                    self._release_occupied(occupied)
                    return REASON_AGENT
                self._agent[agent_id] = cur
                occupied.append((self._agent, agent_id))

        if base_dims and self._max_running > 0:
            cur = self._global + 1
            if cur > self._max_running:
                self._release_occupied(occupied)
                return REASON_GLOBAL
            self._global = cur

        return None

    @staticmethod
    def _release_occupied(occupied: list[tuple[dict[str, int], str]]) -> None:
        """回滚本次已 INCR 的全部计数（防名额泄漏）。"""
        for store, key in occupied:
            remaining = store.get(key, 1) - 1
            if remaining <= 0:
                store.pop(key, None)
            else:
                store[key] = remaining

    # ── 注册与释放 ───────────────────────────────────────────────

    async def reserve(self, session_id: str) -> None:
        """登记"本轮已占位、待转正"。

        占位（入口）与登记（响应之后）分两步，中间 run 可能已经结束：
        该情形由 ``release_session`` 记提前结束标记，``register`` 见到
        标记即退掉占位，不再登记。
        """
        if not session_id:
            return
        self._pending[session_id] = self._pending.get(session_id, 0) + 1
        self._prune_early()

    async def register(
        self, session_id: str, user_id: str, agent_id: str = "",
    ) -> None:
        """把待转正的占位转正为"该会话有一轮在跑"，并启动锁观察。

        提前结束标记存在（run 已结束）→ 退掉本轮占位，不再登记（这一轮
        已经没有在跑的名额可占）。
        """
        if not session_id:
            return
        self._pending.pop(session_id, None)
        if session_id in self._early:
            del self._early[session_id]
            await self.rollback("", user_id, agent_id)
            return
        self._registry[session_id] = (user_id, agent_id, time.time())
        self._spawn_watch(session_id, user_id, agent_id)

    async def release_session(self, session_id: str) -> bool:
        """run 结束：释放该会话占的名额。返回是否处理到占位。

        已登记 → 直接释放；仅占位未登记（run 秒失败，登记还没来得及发生）
        → 记提前结束标记，等登记时退掉占位；两者皆无 → 无操作（本次 run
        没有对应的占位，例如框架内部派生的续跑）。
        """
        if not session_id:
            return False
        entry = self._registry.pop(session_id, None)
        if entry is not None:
            self._release_entry(entry)
            return True
        if self._pending.get(session_id):
            self._early[session_id] = time.time()
            return True
        return False

    def _prune_early(self) -> None:
        """清理过期的提前结束标记（避免长期占用内存）。"""
        if not self._early:
            return
        deadline = time.time() - _EARLY_TTL_SECS
        for sid in [s for s, ts in self._early.items() if ts < deadline]:
            del self._early[sid]

    def _spawn_watch(self, session_id: str, user_id: str, agent_id: str) -> None:
        task = asyncio.create_task(
            self._watch_run(session_id, user_id, agent_id),
        )
        self._watch_tasks.add(task)
        task.add_done_callback(self._watch_tasks.discard)

    async def _watch_run(
        self, session_id: str, user_id: str, agent_id: str,
    ) -> None:
        """一次性伴随任务：等 run 的会话锁出现，锁消失即释放名额。

        - 锁在 ``watch_timeout`` 内始终未出现（装配失败/进程内异常）→
          兜底释放，宁可短暂少算不可单向泄漏；
        - 注册表条目已被 ``rollback`` 清掉 → 名额已退，直接退出。
        """
        if self._lock_checker is None:
            return
        try:
            deadline = time.monotonic() + self._watch_timeout
            lock_seen = False
            while time.monotonic() < deadline:
                if session_id not in self._registry:
                    return
                if await self._lock_checker(session_id):
                    lock_seen = True
                    break
                await asyncio.sleep(self._watch_interval)
            if not lock_seen:
                self._release(session_id, user_id, agent_id)
                return
            while True:
                if session_id not in self._registry:
                    return
                if not await self._lock_checker(session_id):
                    self._release(session_id, user_id, agent_id)
                    return
                await asyncio.sleep(self._watch_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail-open：观察失败不阻断任何路径
            logger.exception("memory guard watch task failed for %r", session_id)

    def _release(self, session_id: str, user_id: str, agent_id: str) -> None:
        """释放一个 run 的全部名额（幂等：条目已不在即说明释放过）。"""
        entry = self._registry.pop(session_id, None)
        if entry is None:
            return
        self._release_entry(entry)

    def _release_entry(self, entry: tuple[str, str, float]) -> None:
        """按登记信息退掉三维计数（global / user / agent 各一）。"""
        user_id, agent_id, _ts = entry
        self._global = max(0, self._global - 1)
        self._release_occupied([(self._user, user_id)])
        if agent_id:
            self._release_occupied([(self._agent, agent_id)])

    async def rollback(
        self, session_id: str, user_id: str, agent_id: str = "",
    ) -> None:
        """释放名额（非 2xx 响应 / 注册失败 / 提前结束时调用）。

        无条件退计数（与 Redis 版语义一致：调用方只要求"把占的名额退掉"，
        不要求注册表里真有该条目——非 2xx 时名额已占但从未注册过）；
        顺手清掉待转正与提前结束标记，避免登记阶段重复退。
        """
        self._registry.pop(session_id, None)
        self._pending.pop(session_id, None)
        self._early.pop(session_id, None)
        self._global = max(0, self._global - 1)
        self._release_occupied([(self._user, user_id)])
        if agent_id:
            self._release_occupied([(self._agent, agent_id)])

    async def reconcile(self, grace_secs: float = 0.0) -> int:
        """对账接口（与 Redis 版同签名）：单进程无需对账，恒返回 0。"""
        del grace_secs
        return 0

    async def reconcile_on_startup(self) -> None:
        """启动重建（与 Redis 版同签名）：进程重启计数已归零，无需重建。"""

    async def close(self) -> None:
        """取消并等待全部观察任务（进程关闭/测试清理用）。"""
        tasks = list(self._watch_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
