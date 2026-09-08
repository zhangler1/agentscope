# -*- coding: utf-8 -*-
"""per-tool 持久化中间件测试(fake agent/next_handler,不碰真实框架)。"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolResponse

from bocomadp.config.app_config import ToolResultConfig
from bocomadp.middleware.tool_result_persistence import (
    ToolResultPersistenceMiddleware,
)


class _FakeAgent:
    def __init__(self, session_id: str = "s1") -> None:
        self.state = type("State", (), {"session_id": session_id})()
        self.toolkit = AsyncMock()
        self.toolkit.get_tool = AsyncMock(return_value=None)


def _response(text: str, tool_state=ToolResultState.SUCCESS) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(text=text)],
        state=tool_state,
        id="resp-1",
    )


def _make_next_handler(items):
    """构造 async generator next_handler,输出给定 items。"""

    async def _next(**kwargs):
        for item in items:
            yield item

    return _next


async def _run_mw(mw, agent, items, tool_call):
    out = []
    async for item in mw.on_acting(
        agent,
        {"tool_call": tool_call},
        _make_next_handler(items),
    ):
        out.append(item)
    return out


class TestToolResultPersistenceMiddleware(IsolatedAsyncioTestCase):
    async def test_passthrough_when_disabled(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="bash", input="")
        with patch(
            "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
            new=AsyncMock(return_value=ToolResultConfig(enabled=False)),
        ):
            out = await _run_mw(mw, agent, [_response("x" * 100000)], tool_call)

        assert len(out) == 1
        assert out[0].content[0].text == "x" * 100000  # 原样透传

    async def test_passthrough_when_small(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="bash", input="")
        with patch(
            "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
            new=AsyncMock(return_value=ToolResultConfig()),
        ):
            out = await _run_mw(mw, agent, [_response("small")], tool_call)

        assert out[0].content[0].text == "small"

    async def test_passthrough_for_exempt_tool(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="big_output_tool", input="")
        with patch(
            "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
            new=AsyncMock(return_value=ToolResultConfig(exempt_tools=["big_output_tool"])),
        ):
            out = await _run_mw(mw, agent, [_response("x" * 100000)], tool_call)

        assert out[0].content[0].text == "x" * 100000

    async def test_large_result_persisted_and_replaced(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="big_output_tool", input="")
        big = "x" * 60000
        with (
            patch(
                "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig()),
            ),
            patch(
                "bocomadp.middleware.tool_result_persistence.set_tool_result",
                new=AsyncMock(return_value="tool_result:s1:tc-1"),
            ) as setter,
        ):
            out = await _run_mw(mw, agent, [_response(big)], tool_call)

        setter.assert_awaited_once_with("s1", "tc-1", big)
        replaced = out[0].content[0].text
        assert replaced.startswith("<persisted-output>")
        assert "tool_result:s1:tc-1" in replaced
        assert len(replaced) < 5000  # 预览远小于原文

    async def test_redis_failure_passthrough(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="big_output_tool", input="")
        with (
            patch(
                "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig()),
            ),
            patch(
                "bocomadp.middleware.tool_result_persistence.set_tool_result",
                new=AsyncMock(side_effect=ConnectionError("redis down")),
            ),
        ):
            out = await _run_mw(mw, agent, [_response("x" * 60000)], tool_call)

        assert out[0].content[0].text == "x" * 60000  # 降级透传

    async def test_image_result_passthrough(self):
        from agentscope.message import Base64Source, DataBlock

        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="image_tool", input="")
        response = ToolResponse(
            content=[
                DataBlock(
                    name="img",
                    source=Base64Source(data="", media_type="image/png"),
                ),
            ],
            state=ToolResultState.SUCCESS,
            id="resp-1",
        )
        with patch(
            "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
            new=AsyncMock(return_value=ToolResultConfig()),
        ):
            out = await _run_mw(mw, agent, [response], tool_call)

        assert out[0].content[0].type == "data"  # 原样透传

    async def test_persisted_preview_content_not_repersisted(self):
        """输出以 <persisted-output> 开头 → 透传不持久化(即使超阈值)。"""
        from bocomadp.tool_result_store import (
            PERSISTED_OUTPUT_CLOSING_TAG,
            PERSISTED_OUTPUT_TAG,
        )

        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="echo_tool", input="")
        preview = (
            f"{PERSISTED_OUTPUT_TAG}\n输出过大(58.6KB),完整内容已保存至: "
            f"tool_result:s1:tc-1\n预览(前 2000 字符):\nxxxx\n...\n"
            f"{PERSISTED_OUTPUT_CLOSING_TAG}"
        )
        with (
            patch(
                "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig()),
            ),
            patch(
                "bocomadp.middleware.tool_result_persistence.set_tool_result",
                new=AsyncMock(return_value="tool_result:s1:tc-1"),
            ) as setter,
        ):
            out = await _run_mw(mw, agent, [_response(preview)], tool_call)

        setter.assert_not_awaited()
        assert out[0].content[0].text == preview  # 原样透传

    async def test_streaming_chunks_forwarded_then_replaced(self):
        agent = _FakeAgent()
        mw = ToolResultPersistenceMiddleware()
        tool_call = ToolCallBlock(id="tc-1", name="big_output_tool", input="")
        chunks = [
            ToolChunk(content=[TextBlock(text="part1")], state=ToolResultState.RUNNING),
            ToolResponse(
                content=[TextBlock(text="y" * 60000)],
                state=ToolResultState.SUCCESS,
                id="resp-1",
            ),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_persistence.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig()),
            ),
            patch(
                "bocomadp.middleware.tool_result_persistence.set_tool_result",
                new=AsyncMock(return_value="tool_result:s1:tc-1"),
            ),
        ):
            out = []
            async for item in mw.on_acting(
                agent,
                {"tool_call": tool_call},
                _make_next_handler(chunks),
            ):
                out.append(item)

        assert out[0].content[0].text == "part1"  # 中间块透传
        assert out[1].content[0].text.startswith("<persisted-output>")  # 终块替换
