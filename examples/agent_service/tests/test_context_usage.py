"""上下文窗口占用链路单测。

覆盖三段：

1. :class:`ContextUsageMiddleware` —— 按 ``reply_id`` 记录最后一次模型
   调用的 usage（input/output 均为 latest-wins、网关缺 usage 时的守卫）；
2. :func:`attach_context_usage` —— 落库前写入
   ``msg.metadata["context_usage"]``，且能随 ``model_dump`` 落库；
3. 读取侧 —— ``session_usage`` / ``deerflow threads`` 优先取窗口占用、
   回退历史累加值；``DeerflowSSEFormatter`` 的流式口径与之一致。

背景：框架 ``Msg.append_event`` 把一个 reply 内所有 ``MODEL_CALL_END``
的 usage 累加（官方设计，消息粒度），多轮 ReAct 后 ``usage.input_tokens``
是 N 次请求之和而非上下文窗口大小。本链路各环节统一改为：input / output
都取最后一次调用，共同描述该消息落定时上下文窗口的占用。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from agentscope.message import AssistantMsg, Msg, TextBlock, Usage

from bocomadp.deerflow.formatter import DeerflowSSEFormatter
from bocomadp.deerflow.routers.threads import _usage_metadata_of
from bocomadp.middleware.custom.context_usage import (
    _LAST_CALL_USAGE,
    ContextUsageMiddleware,
    attach_context_usage,
    pop_context_usage,
)
from bocomadp.routers.session_usage import _context_usage_of

REPLY_ID = "reply-1"


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    """模拟 ``ChatUsage``（本链路只用到两个 token 字段）。"""
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def _msg(usage=None, metadata=None, msg_id: str = REPLY_ID) -> Msg:
    return AssistantMsg(
        name="agent",
        content=[TextBlock(text="hi")],
        id=msg_id,
        usage=usage,
        metadata=metadata,
    )


class _FakeAgent:
    """只暴露中间件需要的 ``state.reply_id``。"""

    def __init__(self, reply_id: str = REPLY_ID) -> None:
        self.state = SimpleNamespace(reply_id=reply_id)


class _FakeStream:
    """模拟框架流式返回 ``AsyncGenerator[ChatResponse, None]``。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


async def _drain(agen) -> list[Any]:
    return [chunk async for chunk in agen]


def _handler(chunks: list[Any]):
    async def _next():
        return _FakeStream(chunks)

    return _next


class TestContextUsageMiddleware(unittest.IsolatedAsyncioTestCase):
    """中间件：单 reply 内多次调用只保留最后一次窗口值。"""

    def setUp(self) -> None:
        _LAST_CALL_USAGE.clear()

    async def test_streaming_passes_chunks_and_records_usage(self) -> None:
        """流式：chunk 原样透传，收尾 usage 被记录。"""
        mw = ContextUsageMiddleware()
        chunks = [
            SimpleNamespace(usage=None),  # 普通增量 chunk
            SimpleNamespace(usage=_usage(100, 10)),  # 收尾 usage chunk
        ]

        stream = await mw.on_model_call(_FakeAgent(), {}, _handler(chunks))
        self.assertEqual(await _drain(stream), chunks)

        self.assertEqual(
            pop_context_usage(REPLY_ID),
            {"input_tokens": 100, "output_tokens": 10, "calls": 1},
        )

    async def test_both_fields_take_the_last_call_only(self) -> None:
        """多轮：input / output 都覆盖（= 落定时上下文窗口占用）。"""
        mw = ContextUsageMiddleware()
        agent = _FakeAgent()

        await _drain(await mw.on_model_call(
            agent, {}, _handler([SimpleNamespace(usage=_usage(100, 10))]),
        ))
        await _drain(await mw.on_model_call(
            agent, {}, _handler([SimpleNamespace(usage=_usage(250, 20))]),
        ))

        self.assertEqual(
            _LAST_CALL_USAGE[REPLY_ID],
            {"input_tokens": 250, "output_tokens": 20, "calls": 2},
        )

    async def test_zero_usage_keeps_previous_values(self) -> None:
        """网关未下发 usage（0/0）时保留上一轮有效值，不刷成 0。"""
        mw = ContextUsageMiddleware()
        agent = _FakeAgent()

        await _drain(await mw.on_model_call(
            agent, {}, _handler([SimpleNamespace(usage=_usage(250, 20))]),
        ))
        await _drain(await mw.on_model_call(
            agent, {}, _handler([SimpleNamespace(usage=_usage(0, 0))]),
        ))

        self.assertEqual(
            _LAST_CALL_USAGE[REPLY_ID],
            {"input_tokens": 250, "output_tokens": 20, "calls": 2},
        )

    async def test_non_streaming_is_recorded_and_returned_as_is(self) -> None:
        """非流式：结果对象原样返回，同时记录 usage。"""
        mw = ContextUsageMiddleware()
        response = SimpleNamespace(usage=_usage(300, 30), content=[])

        async def _next():
            return response

        result = await mw.on_model_call(_FakeAgent(), {}, _next)
        self.assertIs(result, response)
        self.assertEqual(
            pop_context_usage(REPLY_ID),
            {"input_tokens": 300, "output_tokens": 30, "calls": 1},
        )

    async def test_missing_reply_id_is_ignored(self) -> None:
        """拿不到 reply_id 时不影响透传（不记录）。"""
        mw = ContextUsageMiddleware()
        agent = SimpleNamespace(state=SimpleNamespace(reply_id=""))
        chunks = [SimpleNamespace(usage=_usage(100, 10))]

        stream = await mw.on_model_call(agent, {}, _handler(chunks))
        self.assertEqual(await _drain(stream), chunks)
        self.assertEqual(_LAST_CALL_USAGE, {})

    def test_registered_by_custom_scanner(self) -> None:
        """custom 目录注册依据：实例带标记且实现了 on_model_call。"""
        mw = ContextUsageMiddleware()
        self.assertTrue(getattr(mw, "_is_agent_middleware", False))
        self.assertTrue(mw.is_implemented("on_model_call"))


class TestAttachContextUsage(unittest.TestCase):
    """落库前把窗口占用写进 ``msg.metadata``（框架可正常往返）。"""

    def setUp(self) -> None:
        _LAST_CALL_USAGE.clear()

    def test_writes_into_metadata_and_keeps_existing_keys(self) -> None:
        _LAST_CALL_USAGE[REPLY_ID] = {
            "input_tokens": 500,
            "output_tokens": 5,
            "calls": 3,
        }
        msg = _msg(metadata={"foo": "bar"})

        self.assertTrue(attach_context_usage(msg))
        self.assertEqual(
            msg.metadata,
            {
                "foo": "bar",
                "context_usage": {
                    "input_tokens": 500,
                    "output_tokens": 5,
                    "calls": 3,
                },
            },
        )
        # 取走即清理，避免异常路径下条目长期驻留
        self.assertEqual(_LAST_CALL_USAGE, {})

    def test_survives_model_dump(self) -> None:
        """写入的字段必须能随 ``model_dump`` 落进 payload。"""
        _LAST_CALL_USAGE[REPLY_ID] = {
            "input_tokens": 500,
            "output_tokens": 5,
            "calls": 3,
        }
        msg = _msg()
        attach_context_usage(msg)

        payload = msg.model_dump(mode="json")
        self.assertEqual(
            payload["metadata"]["context_usage"]["input_tokens"],
            500,
        )
        # 框架的累加口径原样保留，两种口径并存
        self.assertIn("usage", payload)

    def test_no_record_leaves_message_untouched(self) -> None:
        """无记录（用户消息 / 失败上报）不改动消息。"""
        msg = _msg(metadata={"foo": "bar"})
        self.assertFalse(attach_context_usage(msg))
        self.assertEqual(msg.metadata, {"foo": "bar"})


class TestReadSideCollapse(unittest.TestCase):
    """读取侧：只认 ``metadata.context_usage``，不回退累加口径。"""

    def test_reads_context_usage(self) -> None:
        msg = _msg(
            # 框架累加值：15 轮之和，读取侧不采用
            usage=Usage(input_tokens=1540012, output_tokens=13178),
            # 窗口占用：最后一次调用的值
            metadata={
                "context_usage": {
                    "input_tokens": 111648,
                    "output_tokens": 967,
                    "calls": 15,
                },
            },
        )

        self.assertEqual(_context_usage_of(msg), (111648, 967))
        self.assertEqual(
            _usage_metadata_of(msg),
            {
                "input_tokens": 111648,
                "output_tokens": 967,
                "total_tokens": 112615,
            },
        )

    def test_no_fallback_to_accumulated_usage(self) -> None:
        """历史消息（无 context_usage）不给值，不采用框架累加口径。"""
        legacy = _msg(usage=Usage(input_tokens=500, output_tokens=50))

        self.assertEqual(_context_usage_of(legacy), (0, 0))
        self.assertIsNone(_usage_metadata_of(legacy))

    def test_zero_when_message_has_no_usage(self) -> None:
        empty = _msg()

        self.assertEqual(_context_usage_of(empty), (0, 0))
        self.assertIsNone(_usage_metadata_of(empty))


class TestEndToEndChain(unittest.IsolatedAsyncioTestCase):
    """中间件 → 落库注入 → 读取：同一组数字走完全程。"""

    def setUp(self) -> None:
        _LAST_CALL_USAGE.clear()

    async def test_three_round_tool_loop_yields_window_usage(self) -> None:
        """模拟一次 3 轮工具循环：落库值 = 最后一次调用，而非 3 轮之和。"""
        mw = ContextUsageMiddleware()
        agent = _FakeAgent()

        rounds = [(13331, 101), (13400, 55), (13845, 210)]
        for input_tokens, output_tokens in rounds:
            await _drain(await mw.on_model_call(
                agent,
                {},
                _handler([SimpleNamespace(usage=_usage(input_tokens, output_tokens))]),
            ))

        # 框架 Msg.append_event 会累加三轮（与真实落库前的对象一致）
        msg = _msg(
            usage=Usage(
                input_tokens=sum(r[0] for r in rounds),  # 40576
                output_tokens=sum(r[1] for r in rounds),  # 366
            ),
        )
        self.assertTrue(attach_context_usage(msg))

        payload = msg.model_dump(mode="json")
        context_usage = payload["metadata"]["context_usage"]
        self.assertEqual(context_usage["input_tokens"], 13845)  # 窗口占用
        self.assertEqual(context_usage["output_tokens"], 210)  # 最后一次输出
        self.assertEqual(context_usage["calls"], 3)
        # 框架累加口径原样保留
        self.assertEqual(payload["usage"]["input_tokens"], 40576)
        self.assertEqual(payload["usage"]["output_tokens"], 366)

        # 读取侧给出窗口占用（13845）而不是累加值（40576）
        self.assertEqual(_context_usage_of(msg), (13845, 210))
        self.assertEqual(
            _usage_metadata_of(msg),
            {"input_tokens": 13845, "output_tokens": 210, "total_tokens": 14055},
        )


class TestFormatterMatchesMetadataBasis(unittest.TestCase):
    """SSE 流式口径与落库 metadata 口径一致。"""

    @staticmethod
    def _model_call_end(input_tokens: int, output_tokens: int) -> dict[str, Any]:
        return {
            "type": "MODEL_CALL_END",
            "session_id": "t1",
            "reply_id": REPLY_ID,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "finished_reason": "stop",
            "run_id": "run1",
        }

    def test_usage_matches_context_usage_semantics(self) -> None:
        """三字段与 metadata.context_usage 同口径：都取最后一次调用。"""
        formatter = DeerflowSSEFormatter()
        formatter.translate(self._model_call_end(100, 10))
        formatter.translate(self._model_call_end(250, 20))
        formatter.translate(self._model_call_end(0, 0))  # 网关缺 usage

        self.assertEqual(
            formatter.usage,
            {"input_tokens": 250, "output_tokens": 20, "total_tokens": 270},
        )


if __name__ == "__main__":
    unittest.main()
