# -*- coding: utf-8 -*-
"""per-tool 超长输出持久化中间件(on_acting)。

工具结果字符数超过配置阈值时,完整内容写入 Redis(带 TTL,TTL 实时读
runtime_configs 表 ``tool_result`` 段),模型收到的 tool_result 被替换为
``<persisted-output>`` 预览(大小 + 键 + 2KB 预览 + 读回提示)。

位于框架 ToolOffloadMiddleware 内层:正常完成拿到真实结果流;超时占位符
不会经过本中间件。替换后内容远小于框架 ``tool_result_limit``,框架 token
截断与文件 offload 不会触发(天然兼容);Redis 故障时 catch 后透传原文,
框架截断照常兜底。
"""
from __future__ import annotations

import logging
from typing import Any

from agentscope.message import TextBlock
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolResponse

from ..logging.agent_log_context import ctx_fields
from ..tool_result_message import build_persisted_message, extract_text_content
from ..tool_result_store import (
    PERSISTED_OUTPUT_TAG,
    get_tool_result_config,
    set_tool_result,
)

logger = logging.getLogger("as")


class ToolResultPersistenceMiddleware(MiddlewareBase):  # pylint: disable=abstract-method
    """工具结果超长时持久化到 Redis 并替换为预览(仅文本结果)。"""

    async def on_acting(  # type: ignore[override]
        self,
        agent,
        input_kwargs: dict[str, Any],
        next_handler,
    ):  # -> AsyncGenerator[ToolChunk | ToolResponse, None]
        """工具执行流:透传中间块,终块超限则持久化替换为预览。"""
        tool_call = input_kwargs["tool_call"]
        session_id = agent.state.session_id
        tool_name = tool_call.name
        ctx = ctx_fields(agent)

        try:
            cfg = await get_tool_result_config()
        except Exception as exc:  # pragma: no cover - 配置读取异常兜底
            logger.warning(
                "TOOL_RESULT_PERSIST cfg_failed %s tool=%s tool_call_id=%s err=%s",
                ctx,
                tool_name,
                getattr(tool_call, "id", "-"),
                exc,
            )
            cfg = None

        if cfg is None or not cfg.enabled or tool_name in cfg.exempt_tools:
            async for item in next_handler(**input_kwargs):
                yield item
            return

        final_response: ToolResponse | None = None
        async for item in next_handler(**input_kwargs):
            if isinstance(item, ToolResponse):
                final_response = item
            else:
                # 流式中间块(ToolChunk)原样透传
                yield item

        if final_response is None:
            return

        text = extract_text_content(final_response.content)
        # 已持久化内容检测:输出以 <persisted-output> 开头(预览/历史占位符)
        # → 永不二次持久化(防间接循环,对齐 CC 聚合层 isContentAlreadyCompacted)
        if text is None or text.startswith(PERSISTED_OUTPUT_TAG):
            yield final_response
            return
        if len(text) <= cfg.per_tool_threshold_chars:
            yield final_response
            return

        try:
            key = await set_tool_result(session_id, tool_call.id, text)
        except Exception as exc:
            # 降级:透传原文,框架 token 截断兜底(绝不比现状更差)
            logger.warning(
                "TOOL_RESULT_PERSIST persist_failed %s tool=%s tool_call_id=%s "
                "size=%d err=%s",
                ctx,
                tool_name,
                getattr(tool_call, "id", "-"),
                len(text),
                exc,
            )
            yield final_response
            return

        logger.info(
            "TOOL_RESULT_PERSIST done %s tool=%s tool_call_id=%s size=%d key=%s",
            ctx,
            tool_name,
            getattr(tool_call, "id", "-"),
            len(text),
            key,
        )

        max_output_chars = min(
            cfg.read_result_max_output_chars,
            cfg.per_tool_threshold_chars,
        )
        preview_msg = build_persisted_message(
            key,
            len(text),
            text,
            cfg.preview_chars,
            tool_call_id=tool_call.id,
            max_output_chars=max_output_chars,
        )
        yield ToolResponse(
            content=[TextBlock(text=preview_msg)],
            state=final_response.state,
            metadata=final_response.metadata,
            id=final_response.id,
        )
