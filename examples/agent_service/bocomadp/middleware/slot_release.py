# -*- coding: utf-8 -*-
"""SlotReleaseMiddleware —— run 结束无条件软释放温池 slot。

配合 ``SharedPvcK8sWorkspace.release_slot`` 使用：``on_reply`` 洋葱钩子
在整次 reply（run）结束后执行 finally，把本会话占用的温池 slot 软释放
（Pod 标签 → ``released-{session}``），网关与 backend 保持存活。下次
run 经 manager 快路径重挂载（百毫秒级）；池紧张时 slot 可立即被其他
会话抢占（抢占方走完整初始化）。

不检查后台任务：软释放的 slot 即使被抢占，抢占方 initialize 经覆写的
``_setup_mcp_gateway`` 健康检查直接复用仍在运行的网关（不 pkill），
后台任务持有的网关句柄不受影响；仅当网关本身已不健康才会重启，
此时后台任务本已失败。固定释放也让 released 状态对所有实例可见，
同一会话跨实例任意时刻最多占用一个 slot，不会累积占坑。

run 期间对 workspace 置 busy 标记（``set_run_active``），配合
``SharedPvcK8sWorkspaceManager._sweep_once`` 的续期逻辑：超过 TTL
的长 run 不会被 sweeper close 拆 backend，工具执行不受影响。
"""
from __future__ import annotations

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
    """run 结束软释放温池 slot（见模块 docstring）。"""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> AsyncGenerator:
        """包一层 next_handler：run 全程置 busy，结束后释放 slot。

        busy 标记配合 ``SharedPvcK8sWorkspaceManager._sweep_once``：
        超 TTL 的长 run 不会被 sweeper close（条目续期），工具执行
        不受影响。``finally`` 在生成器正常结束或被 close（中断/
        提前断开）时都会执行，因此用户中断、HITL 暂停、正常完成
        都能触发释放；HITL 挂起期间标记保持 True，sweeper 持续
        续期，不会在用户确认前回收。
        """
        try:
            self._mark_run_active(agent, True)
            async for item in next_handler(**input_kwargs):
                yield item
        finally:
            await self._release(agent)
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

    async def _release(self, agent: Any) -> None:
        """无条件软释放本会话的温池 slot。"""
        offloader = getattr(agent, "offloader", None)
        release = getattr(offloader, "release_slot", None)
        if release is None:
            # 本地模式 / 按需模式 workspace 没有软释放概念
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
