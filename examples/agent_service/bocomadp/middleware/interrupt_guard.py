# -*- coding: utf-8 -*-
"""Interrupt 守卫（examples 侧，零框架侵入）。

框架 interrupt 链路存在三个缺口，本模块通过订阅 interrupt 广播
频道（与 CancelDispatcher 同频道）+ reasoning 边界检查补齐：

1. interrupt 不取消后台工具任务（原 _cancel_dispatcher 改动的
   等价物）：订阅循环直接调 background_task_manager.cancel_session_tasks。
2. 后台工具结果把已暂停 session 复活（原 _tool_offload_middleware
   守卫的等价物）：复活 run 的 inputs=None，on_reply 不清标记 →
   首轮 on_reasoning 立即终止。
3. 并发工具执行器吞掉 CancelledError 导致 agent 停不下来（新问题，
   竞态窗口：取消到达时工具恰好已成功完成 → 无 INTERRUPTED 事件
   → uncancel 吞掉一次性取消信号）：on_reasoning 检测标记后补发
   CancelledError，复用框架 _reply_impl 现成的优雅中断收尾。

注意：绝不能在 on_acting 里补枪——并发批次时 on_acting 跑在
gather(return_exceptions=True) 的 worker 里，CancelledError 会被
吞掉且留下无结果的脏 tool_call。
"""
import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Callable

from agentscope.app.message_bus import MessageBusKeys
from agentscope.event import (
    ExternalExecutionResultEvent,
    UserConfirmResultEvent,
)
from agentscope.message import Msg
from agentscope.middleware import MiddlewareBase

_logger = logging.getLogger("as")


_FLAG_TTL_SECS = 24 * 3600
_RECANCEL_AFTER_SECS = 5.0


class InterruptGuardState:
    """进程级单例：订阅 interrupt 广播，记录标记 + 取消 BG 任务。"""

    def __init__(self) -> None:
        self._app: Any = None          # FastAPI app，懒解析 app.state
        self._interrupted_at: dict[str, float] = {}
        self._sub_task: asyncio.Task | None = None
        # 看门狗 task 强引用：create_task 返回值若被丢弃，事件循环只持
        # 弱引用，任务可能被 GC 中途回收（5s sleep 期间风险真实存在）。
        self._watchdog_tasks: set[asyncio.Task] = set()

    def configure(self, app: Any) -> None:
        """main.py 在 create_app 之后调用一次。"""
        self._app = app

    async def ensure_started(self) -> None:
        """幂等启动订阅循环（首个 run 的 on_reply 触发）。

        时序保证：interrupt 只在 session 锁被持有（run 进行中）时发布，
        必然晚于该进程首个 run 的 on_reply → 订阅必然先于任何需要它的
        interrupt 生效。
        等待 on_ready 事件：subscribe 的队列注册发生在生成器首次调度
        时，若不等待，订阅建立前发布的 interrupt 会丢失（对齐框架
        CancelDispatcher 的 ready-event 模式）。
        """
        if self._sub_task is not None or self._app is None:
            return
        ready = asyncio.Event()####创建一个信号标识
        #####create_task只是将任务放入待执行队列，并不会立即执行，通过将标识传进_subscribe_loop
        ########当其在messagebus中创建好queue后，会将标识点亮，然后await ready.wait()放行
        ##保证订阅完全准备好
        self._sub_task = asyncio.get_running_loop().create_task(
            self._subscribe_loop(ready),
            name="interrupt-guard",
        )
        await ready.wait()

    def _on_interrupt(self, session_id: str) -> None:
        #####将当前会话记录为中断
        self._interrupted_at[session_id] = time.monotonic()
        _logger.info(
            "InterruptGuard: recorded interrupt for session %s",
            session_id,
        )

        # ── 缺口 1：取消本进程该 session 的后台工具任务 ──
        # （框架原生 _interrupt_session 只取消 chat-run task）
        try:
            bg = self._app.state.background_task_manager
            n = bg.cancel_session_tasks(session_id)
            if n:
                _logger.info(
                    "InterruptGuard: cancelled %d BG task(s) for "
                    "session %s",
                    n,
                    session_id,
                )
        except Exception:  # noqa: BLE001 —— 兜底路径绝不影响主流程
            _logger.exception(
                "InterruptGuard: BG cancel failed for session %s",
                session_id,
            )

        # ── 缺口 3 兜底：5 秒看门狗二次取消 ──
        # 覆盖任何未知的吞取消路径；正常中断毫秒级结束，watchdog 是 no-op。
        async def _watchdog() -> None:
            await asyncio.sleep(_RECANCEL_AFTER_SECS)
            try:
                t = self._app.state.chat_run_registry.get(session_id)
                if t is not None and not t.done():
                    _logger.warning(
                        "InterruptGuard: interrupt not effective in %.0fs, "
                        "re-cancelling chat run for session %s",
                        _RECANCEL_AFTER_SECS,
                        session_id,
                    )
                    t.cancel()
            except Exception:  # noqa: BLE001 —— 看门狗绝不外抛
                _logger.exception(
                    "InterruptGuard: watchdog failed for session %s",
                    session_id,
                )

        watchdog = asyncio.get_running_loop().create_task(_watchdog())
        self._watchdog_tasks.add(watchdog)##########添加到集合中，防止被回收
        ####add_done_callback(discard) = task 结束时自动调
        #self._watchdog_tasks.discard(watchdog)，把自己从集合里删掉。
        watchdog.add_done_callback(self._watchdog_tasks.discard)

    async def _subscribe_loop(self, ready: asyncio.Event) -> None:
        try:
            async for payload in self._app.state.message_bus.subscribe(
                MessageBusKeys.session_interrupt_channel(),
                on_ready=ready.set,
            ):
                sid = payload.get("session_id")
                if isinstance(sid, str):
                    self._on_interrupt(sid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.exception("InterruptGuard: subscribe loop crashed")
        finally:
            # subscribe 在 on_ready 前失败时也解除 ensure_started 的
            # 等待，避免启动死锁（对齐框架 CancelDispatcher 的模式）。
            ready.set()

    def is_interrupted(self, session_id: str) -> bool:
        ts = self._interrupted_at.get(session_id)
        if ts is None:
            return False
        if time.monotonic() - ts > _FLAG_TTL_SECS:
            self._interrupted_at.pop(session_id, None)
            return False
        return True

    def clear(self, session_id: str) -> None:
        self._interrupted_at.pop(session_id, None)


interrupt_guard_state = InterruptGuardState()


class InterruptGuardMiddleware(MiddlewareBase):  # pylint: disable=abstract-method
    """reasoning 边界检测被吞掉的 interrupt 并补枪。"""

    async def on_reply(  # type: ignore[override]
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        await interrupt_guard_state.ensure_started()
        # 真实用户动作（新消息 / HITL 确认续跑 / 外部结果）→ 清标记。
        # inputs=None（wake 唤醒，即 BG 工具结果复活）与
        # UserInterruptEvent（挂起态中断收尾）不清 → 缺口 2 的复活
        # run 会在首轮 reasoning 被立即终止。
        inputs = input_kwargs.get("inputs")
        if isinstance(
            inputs,
            (Msg, list, UserConfirmResultEvent, ExternalExecutionResultEvent),
        ):
            interrupt_guard_state.clear(agent.state.session_id)
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_reasoning(  # type: ignore[override]
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        sid = agent.state.session_id
        if interrupt_guard_state.is_interrupted(sid):
            _logger.warning(
                "InterruptGuard: re-applying swallowed interrupt at "
                "reasoning boundary: session=%s",
                sid,
            )
            # 复用框架 _reply_impl 的优雅中断路径：
            # ReplyEndEvent(INTERRUPTED) + 中断消息 + 关闭未完成工具调用
            raise asyncio.CancelledError()
        async for evt in next_handler(**input_kwargs):
            yield evt
