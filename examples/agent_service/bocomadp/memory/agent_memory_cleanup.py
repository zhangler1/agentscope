# -*- coding: utf-8 -*-
"""服务模式：删除智能体后清理其记忆数据（配置 + 会话运行时状态）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("as")


class AgentMemoryCleanupMiddleware:
    """在 ``DELETE /agent/{agent_id}`` 返回 204 后清理该 agent 的记忆数据。"""

    app: Any

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") != "DELETE":
            await self.app(scope, receive, send)
            return

        # 用容器承接 send 回调里写入的状态码（直接赋局部变量会被静态分析
        # 认为始终为 None，进而把后续逻辑误判为不可达）
        response_state: dict[str, int | None] = {"status": None}

        async def _send_with_status(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_state["status"] = message.get("status")
            await send(message)

        await self.app(scope, receive, _send_with_status)

        if response_state["status"] != 204:
            return

        # 兼容 /agent/{id}、/api/agent/{id}、/agent/{id}/
        segments = [s for s in str(scope.get("path", "")).split("/") if s]
        if len(segments) < 2 or segments[-2] != "agent":
            return
        agent_id = segments[-1]

        try:
            # 延迟导入：避免包装配入口与本模块循环依赖
            from . import cleanup_agent_memory

            await cleanup_agent_memory(agent_id)
            logger.info("memory: cleaned data of deleted agent %s", agent_id)
        except Exception:  # noqa: BLE001 — 清理失败不影响已完成的删除
            logger.exception(
                "memory: cleanup after deleting agent %s failed",
                agent_id,
            )


__all__ = ["AgentMemoryCleanupMiddleware"]
