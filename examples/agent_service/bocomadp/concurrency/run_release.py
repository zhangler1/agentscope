# -*- coding: utf-8 -*-
"""/chat 并发名额的释放信号：挂在 ``ChatService.run`` 的 finally 上。

闸机要回答的问题是"这一轮对话跑完了吗"。框架的会话锁只是"run 正在跑"
的旁证：run 秒失败（模型没配、凭证失效、参数 404 等）时锁只存在几毫秒，
锁观察任务大概率错过，于是名额要等兜底超时才退，期间该智能体的所有新
请求都被限流拒绝。

本模块把"run 结束"变成第一手信号：``ChatService.run`` 返回即代表这一轮
对话结束（成功、失败、异常都一样——框架在 run 内部吞掉异常），在 finally
里通知闸机释放该会话占的名额。锁观察降级为兜底（run 被强杀、进程崩溃
等无法进入 finally 的情形）。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("bocomadp.concurrency.run_release")

_installed = False
_original_run: Any = None


def _wrap_run(
    original_run: Callable[..., Awaitable[Any]],
    guard: Any,
) -> Callable[..., Awaitable[Any]]:
    """包装 run 原实现：结束（含异常）时通知闸机释放该会话的名额。

    ``release_session`` 只认 session_id——闸机按会话登记占位，释放时按
    会话定位，与"这一轮是谁发起的"无关。
    """

    async def _run(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        input_msg: Any = None,
        run_id: str | None = None,
    ) -> Any:
        try:
            return await original_run(
                self,
                user_id,
                session_id,
                agent_id,
                input_msg,
                run_id,
            )
        finally:
            try:
                await guard.release_session(session_id)
            except Exception:  # noqa: BLE001 — 释放失败不阻断 run 收尾
                logger.exception(
                    "concurrency release failed for session %s",
                    session_id,
                )

    return _run


def install_run_release(guard: Any) -> None:
    """包装 ``ChatService.run``：run 结束即释放该会话占的并发名额。

    幂等：重复调用只生效一次（保留首次传入的闸机）。
    """
    global _installed, _original_run
    if _installed:
        return
    from agentscope.app._service._chat import ChatService

    _original_run = ChatService.run
    ChatService.run = _wrap_run(_original_run, guard)  # type: ignore[method-assign]
    _installed = True
    logger.info("patched ChatService.run with concurrency release hook")


__all__ = ["install_run_release"]
