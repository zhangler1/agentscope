# -*- coding: utf-8 -*-
"""SlotReleaseMiddleware —— run 期间置 busy 标记，run 结束释放资源。

hash 路由（共享池）下已无 slot 占用语义：释放动作经 ``getattr``
探测 ``release_slot``，方法不存在时自动降级为 no-op（本地模式 /
按需模式同样降级）。保留本中间件的实际意义是 run 全程置 busy
标记（``set_run_active``），配合
``SharedPvcK8sWorkspaceManager._sweep_once``：

- 超过 TTL 的长 run 不会被 sweeper close 拆 backend；
- run 开始/结束时刷新池 Pod 活跃信号（``refresh_active``，K8s
  annotation 全局一致），池空闲回收看到进行中的 run 会跳过，
  多实例部署下同样生效；
- 超过 ``pool_idle_ttl`` 的超长 run 由心跳任务周期性续活跃
  信号（``_spawn_heartbeat``，间隔 ``pool_idle_ttl / 2``），
  避免其他实例的 sweeper 误判“全池闲置”删 Pod。

``finally`` 在生成器正常结束或被 close（中断/提前断开）时都会
执行，因此用户中断、HITL 暂停、正常完成都能触发清理（含心跳
任务取消）；HITL 挂起期间标记保持 True，sweeper 持续续期，不会
在用户确认前回收。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

try:
    from agentscope.middleware import MiddlewareBase
except ImportError:  # pragma: no cover - 无 agentscope 时降级
    class MiddlewareBase:  # type: ignore
        """最小兜底基类：仅在 AgentScope 不可用时使用。"""

        _is_agent_middleware = True

        def is_implemented(self, hook_name: str) -> bool:
            """检查钩子是否被覆写（与框架实现一致）。"""
            base = getattr(MiddlewareBase, hook_name, None)
            sub = getattr(type(self), hook_name, None)
            return base is not sub

        async def list_tools(self) -> list:
            """中间件提供的工具列表。"""
            return []

        async def get_middleware_key(self) -> str:
            """中间件状态键。"""
            return self.__class__.__name__


class SlotReleaseMiddleware(MiddlewareBase):
    """run 期间置 busy 标记，run 结束释放资源（见模块 docstring）。"""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> AsyncGenerator:
        """包一层 next_handler：run 全程置 busy，结束后清理。

        busy 标记配合 ``SharedPvcK8sWorkspaceManager._sweep_once``：
        超 TTL 的长 run 不会被 sweeper close（条目续期），池空闲
        回收也会跳过（``_pool_busy``），工具执行不受影响；超
        ``pool_idle_ttl`` 的超长 run 由心跳任务持续续活跃信号。
        ``finally`` 在生成器正常结束或被 close（中断/提前断开）时
        都会执行，因此用户中断、HITL 暂停、正常完成都能触发
        清理（含心跳任务取消）；HITL 挂起期间标记保持 True，
        sweeper 持续续期。
        """
        heartbeat = self._spawn_heartbeat(agent)
        try:
            self._mark_run_active(agent, True)
            await self._refresh_active(agent)
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            await self._stop_heartbeat(heartbeat)
            await self._release(agent)
            await self._refresh_active(agent)
            self._mark_run_active(agent, False)

    def _mark_run_active(self, agent: Any, active: bool) -> None:
        """置/清 run 使用标记（ws.set_run_active），供 sweeper 续期。

        本地模式 / 按需模式 workspace 没有该方法 → 跳过。
        """
        offloader = getattr(agent, "offloader", None)
        set_active = getattr(offloader, "set_run_active", None)
        if set_active is None:
            return
        try:
            set_active(active)
        except Exception:
            logger.warning(
                "SlotReleaseMiddleware: mark run-active=%s failed "
                "(session=%r)",
                active,
                self._session_id,
                exc_info=True,
            )

    async def _refresh_active(self, agent: Any) -> None:
        """刷新池 Pod 活跃信号（ws.refresh_active），供空闲回收判断。

        run 开始与结束时各刷一次：空闲计时从最后一次 run 结束
        重新起算；annotation 在 K8s 侧全局一致，多实例部署下
        其他实例的 sweeper 也不会误回收进行中的长 run（超
        ``pool_idle_ttl`` 的超长 run 另由心跳任务持续续）。
        本地模式 / 按需模式 workspace 没有该方法 → 跳过。
        """
        offloader = getattr(agent, "offloader", None)
        refresh = getattr(offloader, "refresh_active", None)
        if refresh is None:
            return
        try:
            await refresh()
        except Exception:
            logger.warning(
                "SlotReleaseMiddleware: refresh_active failed "
                "(session=%r)",
                self._session_id,
                exc_info=True,
            )

    def _spawn_heartbeat(self, agent: Any) -> Any | None:
        """spawn 心跳任务：run 期间周期性刷新池 Pod 活跃信号。

        跨实例正确性：池空闲回收的判定依据是 Pod 的 last-active
        annotation（K8s 全局一致）。若只在 run 开始刷一次，超过
        ``pool_idle_ttl`` 的长 run 会被其他实例的 sweeper 误判
        “全池闲置”删 Pod；心跳每隔 ``pool_idle_ttl / 2`` 续一次
        信号即可杜绝。开销与 run 时长成正比：短 run 在首次心跳
        前结束（finally cancel），零额外调用。

        按需模式 / 本地模式没有 ``refresh_heartbeat_interval``
        （为 0）或 ``refresh_active`` → 返回 None 自动降级。
        """
        offloader = getattr(agent, "offloader", None)
        refresh = getattr(offloader, "refresh_active", None)
        if refresh is None:
            return None
        interval = getattr(offloader, "refresh_heartbeat_interval", 0.0)
        if not interval or interval <= 0:
            return None
        return asyncio.create_task(self._heartbeat_loop(refresh, interval))

    async def _heartbeat_loop(self, refresh: Any, interval: float) -> None:
        """心跳循环：睡 interval 后刷一次活跃信号，直至被 cancel。

        refresh 失败静默（仅影响空闲判定，下轮心跳再试）。
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await refresh()
            except Exception:
                logger.warning(
                    "SlotReleaseMiddleware: heartbeat refresh failed "
                    "(session=%r)",
                    self._session_id,
                    exc_info=True,
                )

    async def _stop_heartbeat(self, task: Any | None) -> None:
        """取消并回收心跳任务（run 正常结束/中断/取消都会走到）。"""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _release(self, agent: Any) -> None:
        """释放 run 占用的资源（hash 路由下自动降级为 no-op）。"""
        offloader = getattr(agent, "offloader", None)
        release = getattr(offloader, "release_slot", None)
        if release is None:
            # hash 路由无占用语义 / 本地模式 / 按需模式：
            # 没有 release_slot 方法，自动降级
            return
        try:
            await release()
        except Exception:
            # 释放失败不能影响已完成的 run，TTL sweeper 兜底
            logger.warning(
                "SlotReleaseMiddleware: release failed (session=%r)",
                self._session_id,
                exc_info=True,
            )
