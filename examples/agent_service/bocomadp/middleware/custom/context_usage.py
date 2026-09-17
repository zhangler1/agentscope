# -*- coding: utf-8 -*-
"""上下文窗口占用中间件。

记录每个 reply **最后一次**模型调用的 token 用量——即该次请求发出时
上下文窗口的真实大小（网关返回的 ``prompt_tokens``）。

背景：框架 ``Msg.append_event`` 把一个 reply 内所有 ``MODEL_CALL_END``
的 usage **累加** 成"本轮总消耗"（官方设计，见 ``message/_base.py``：
usage is accumulated across each MODEL_CALL_END）。因此在发生工具调用
循环（多轮 ReAct）时，message payload 里的 ``usage.input_tokens`` 会是
N 次调用之和，远大于单次请求的上下文长度（实测最高放大 13.8 倍）。

而 ``ChatResponse.usage`` 在**单次调用内**是 latest-wins
（``_StreamAccumulator``：``if chat_response.usage: self.usage = ...``），
所以 ``on_model_call`` 能逐次看到真实的窗口值。本中间件按 ``reply_id``
覆盖记录最后一次调用的 usage，交由 ``main.py`` 的 storage proxy 在落库
前写进 ``msg.metadata``（键 ``context_usage``），供会话用量接口与前端消费。

跨进程说明：缓存只在进程内，key 为 ``reply_id``；同一 run 的模型调用与
消息落库必在同一进程（``ChatService`` 持有同一个 storage），因此落库时
总是能命中。取走即清理（``pop_context_usage``），不会长期驻留。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Awaitable, Callable

from agentscope.middleware import MiddlewareBase

logger = logging.getLogger(__name__)

#: ``reply_id`` -> 该 reply **最后一次**模型调用的记录
#: （``input_tokens`` / ``output_tokens`` / ``calls``）。
#: input / output 共同描述该消息落定时上下文窗口的占用，``calls`` 为
#: 该 reply 内的模型调用轮数。
_LAST_CALL_USAGE: dict[str, dict[str, int]] = {}

#: 未落库条目的上限。正常路径下条目在落库时被 take 走；异常路径（reply 在
#: 落库前失败且 msg.id 与 reply_id 不匹配）可能残留，超过上限时丢弃最早的
#: 一条，避免进程长期运行内存无界增长。
_MAX_PENDING: int = 256


def pop_context_usage(reply_id: str) -> dict[str, int] | None:
    """取走并清理某 reply 的窗口占用记录。

    由 storage proxy 在落库时调用：取走即清理，避免异常路径（reply 从未
    落库）导致条目长期驻留。

    Args:
        reply_id (`str`):
            回复 id（== ``msg.id``）。

    Returns:
        `dict[str, int] | None`:
            ``{"input_tokens", "output_tokens", "calls"}`；无记录时
            ``None``。
    """
    return _LAST_CALL_USAGE.pop(reply_id, None)


def _record(reply_id: str, usage: Any) -> None:
    """记录一次模型调用的 usage（口径与 SSE formatter 保持一致）。

    两个 token 字段都**取最后一次**调用（latest-wins）：

    - ``input_tokens`` —— 最后一次调用的 prompt 长度，即当前上下文窗口
      占用。每次调用都会把全部上下文重新作为 prompt 发出，若累加，一次
      工具循环后会变成 N 轮之和（实测最高放大 13.8 倍）；
    - ``output_tokens`` —— 最后一次调用的输出长度；
    - ``calls`` —— 该 reply 内的模型调用轮数。

    网关未下发 usage 时对应字段为 0（实测约 5.6% 的调用），此时保留
    上一轮的有效值，避免把已记录的数字刷成 0。
    """
    prev = _LAST_CALL_USAGE.get(reply_id) or {
        "input_tokens": 0,
        "output_tokens": 0,
        "calls": 0,
    }
    if reply_id not in _LAST_CALL_USAGE and len(_LAST_CALL_USAGE) >= _MAX_PENDING:
        # 异常路径残留兜底（dict 保序，丢弃最早写入的条目）
        _LAST_CALL_USAGE.pop(next(iter(_LAST_CALL_USAGE)), None)

    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    if input_tokens <= 0:
        input_tokens = prev["input_tokens"]
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    if output_tokens <= 0:
        output_tokens = prev["output_tokens"]

    _LAST_CALL_USAGE[reply_id] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "calls": prev["calls"] + 1,
    }


def attach_context_usage(msg: Any) -> bool:
    """把 ``msg`` 对应的窗口占用写进其 ``metadata``（落库前调用）。

    由 ``main.py`` 的 storage proxy 在 ``upsert_message`` 里调用：按
    ``msg.id``（== ``reply_id``）取走中间件记录的最后一次调用值，写入
    ``metadata["context_usage"]``。

    取不到记录时（用户消息、失败上报消息、中间件未覆盖的调用）**不改动
    消息**。原有 ``metadata`` 其它键保留。

    Args:
        msg (`Any`): 待落库的框架 ``Msg`` 实例。

    Returns:
        `bool`: 是否写入（``False`` 表示无记录，消息原样透传）。
    """
    reply_id = getattr(msg, "id", None)
    if not reply_id:
        return False

    context_usage = pop_context_usage(reply_id)
    if context_usage is None:
        return False

    # 必须整体替换：pydantic 不跟踪字典的原地修改。
    msg.metadata = {
        **(getattr(msg, "metadata", None) or {}),
        "context_usage": context_usage,
    }
    return True


class ContextUsageMiddleware(MiddlewareBase):
    """按 ``reply_id`` 记录最后一次模型调用的 usage。

    只实现 ``on_model_call``（``MiddlewareBase.is_implemented`` 依据方法是
    否被覆盖自动识别）；按框架契约返回 ``ChatResponse`` 或
    ``AsyncGenerator[ChatResponse]``，函数本身不是 async generator。
    """

    #: ``MiddlewareRegistry.load_custom`` 注册依据。custom 目录按模块名字母
    #: 序加载（context_usage 早于 event_log），不能依赖后者给基类打标，故
    #: 在本类上直接声明，保证无论加载顺序如何都能被扫描到。
    _is_agent_middleware = True

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """透传模型调用，并记录本次调用的 usage。"""
        result = await next_handler()

        reply_id = getattr(getattr(agent, "state", None), "reply_id", None)
        if not reply_id:
            return result

        # 非流式：结果本身就是完整 ChatResponse
        if not hasattr(result, "__aiter__"):
            usage = getattr(result, "usage", None)
            if usage is not None:
                _record(reply_id, usage)
            return result

        # 流式：透传每个 chunk，结束时记最后一个带 usage 的 chunk
        async def _wrapped() -> AsyncGenerator[Any, None]:
            last_usage = None
            async for chunk in result:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    last_usage = chunk_usage
                yield chunk
            if last_usage is not None:
                _record(reply_id, last_usage)

        return _wrapped()


#: 模块级实例：``MiddlewareRegistry.load_custom`` 扫描 custom/ 包时注册。
context_usage_mw = ContextUsageMiddleware()

__all__ = [
    "ContextUsageMiddleware",
    "context_usage_mw",
    "pop_context_usage",
]
