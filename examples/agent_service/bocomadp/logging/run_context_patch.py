# -*- coding: utf-8 -*-
"""为原生 ``ChatService.run`` 绑定 run_id ContextVar。

框架在 ``run()`` 内部生成 run_id，但只在 **deerflow** 的 ``runs/stream``
路径用 ``run_id_context`` 绑定过；原生 ``/api/chat/`` 与 WakeupDispatcher
的 spawn 都没有绑定。结果是工具执行期间 ``get_current_run_id()`` 为空，
记忆 MCP 的 ``runId`` 请求头（提取接口必需）会缺失。

这里在 bocomadp 层（不改框架源码）包装 ``ChatService.run``：生成/复用
run_id 后用 ``run_id_context`` 包裹整个 run，run 内所有模型/工具调用都能
读到同一个 id。
"""
from __future__ import annotations

import functools
import logging
import uuid

logger = logging.getLogger("as")


def install_run_id_context() -> None:
    """Patch ``ChatService.run``：为每次 run 绑定 run_id ContextVar。"""
    try:
        from agentscope.app._service._chat import ChatService
    except Exception:  # noqa: BLE001 —— 框架不可用时跳过
        logger.exception("run_id_context: ChatService import failed")
        return

    from bocomadp.logging.trace_context import (
        get_current_run_id,
        run_id_context,
    )

    original = ChatService.run
    if getattr(original, "_bocomadp_run_id_ctx", False):
        logger.debug("run_id_context: already installed")
        return

    @functools.wraps(original)
    async def run(self, *args, **kwargs):
        # 兼容位置参数：run(user_id, session_id, agent_id, input_msg, run_id)
        if len(args) >= 5:
            bound = args[4] or get_current_run_id() or uuid.uuid4().hex
            args = (*args[:4], bound, *args[5:])
        else:
            bound = (
                kwargs.get("run_id")
                or get_current_run_id()
                or uuid.uuid4().hex
            )
            kwargs["run_id"] = bound
        with run_id_context(bound):
            return await original(self, *args, **kwargs)

    run._bocomadp_run_id_ctx = True  # type: ignore[attr-defined]
    ChatService.run = run
    logger.info("run_id_context: patched ChatService.run")


__all__ = ["install_run_id_context"]
