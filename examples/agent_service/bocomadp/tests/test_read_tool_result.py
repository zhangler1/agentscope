# -*- coding: utf-8 -*-
"""read_tool_result 读回工具测试。"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from agentscope.message import TextBlock, ToolResultState

from bocomadp.config.app_config import ToolResultConfig
from bocomadp.tools.read_tool_result import read_tool_result_tool


class _FakeState:
    session_id = "s1"


def _call(**kwargs):
    """调用工具 call,注入 _agent_state。"""
    return read_tool_result_tool.call(
        _agent_state=_FakeState(),
        **kwargs,
    )


class TestReadToolResult(IsolatedAsyncioTestCase):
    async def test_returns_full_content(self):
        with patch(
            "bocomadp.tools.read_tool_result.get_tool_result",
            new=AsyncMock(return_value="hello world"),
        ) as getter:
            chunk = await _call(tool_call_id="tc-1")

        getter.assert_awaited_once_with("s1", "tc-1")  # 键由本会话构造
        assert chunk.content[0].text == "hello world"

    async def test_offset_limit_pagination(self):
        with patch(
            "bocomadp.tools.read_tool_result.get_tool_result",
            new=AsyncMock(return_value="0123456789"),
        ) as getter:
            chunk = await _call(tool_call_id="tc-1", offset=2, limit=5)

        getter.assert_awaited_once_with("s1", "tc-1")  # 键由本会话构造
        assert chunk.content[0].text.startswith("23456")
        assert "内容还有 3 字符未读取" in chunk.content[0].text  # 剩余内容 → 中文续读提示

    async def test_limit_over_cap_returns_chinese_pagination_error(self):
        """limit 超过单次输出上限 → 抛错(不切片返回),错误信息中文并强制引导分页。"""
        with (
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result",
                new=AsyncMock(return_value="x" * 5000),
            ),
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result_config",
                new=AsyncMock(
                    return_value=ToolResultConfig(read_result_max_output_chars=1000),
                ),
            ),
        ):
            chunk = await _call(tool_call_id="tc-1", limit=2000)

        text = chunk.content[0].text
        assert "读取范围超过单次输出上限" in text
        assert "1000 字符" in text
        assert "offset" in text and "limit" in text  # 强制引导分页参数
        assert chunk.state == ToolResultState.ERROR

    async def test_dynamic_cap_follows_persist_threshold(self):
        """上限 = min(read_result_max_output_chars, per_tool_threshold_chars),
        阈值配置更小时上限同步收窄(输出恒 ≤ 持久化阈值,防读回循环)。"""
        with (
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result",
                new=AsyncMock(return_value="x" * 5000),
            ),
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result_config",
                new=AsyncMock(
                    return_value=ToolResultConfig(
                        read_result_max_output_chars=100_000,
                        per_tool_threshold_chars=2000,
                    ),
                ),
            ),
        ):
            chunk = await _call(tool_call_id="tc-1", limit=3000)

        assert "2000 字符" in chunk.content[0].text  # 上限 = min(100000, 2000) = 2000

    async def test_pagination_roundtrip_with_continue_hint(self):
        """分段读取拼接后等于完整内容;有剩余时追加中文续读提示,末段无提示。"""
        content = "0123456789"
        with patch(
            "bocomadp.tools.read_tool_result.get_tool_result",
            new=AsyncMock(return_value=content),
        ):
            first = await _call(tool_call_id="tc-1", offset=0, limit=4)
            second = await _call(tool_call_id="tc-1", offset=4, limit=4)
            third = await _call(tool_call_id="tc-1", offset=8, limit=4)

        first_text = first.content[0].text
        assert first_text.startswith("0123")
        assert "内容还有 6 字符未读取" in first_text
        assert "offset=4" in first_text  # 续读提示给出下一次的 offset
        second_text = second.content[0].text
        assert second_text.startswith("4567")
        assert "内容还有 2 字符未读取" in second_text
        assert third.content[0].text == "89"  # 末段无续读提示

    async def test_missing_or_expired_returns_hint(self):
        with (
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bocomadp.tools.read_tool_result.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig(ttl_seconds=14400)),
            ),
        ):
            chunk = await _call(tool_call_id="tc-1")

        assert "已过期或不存在" in chunk.content[0].text
        assert "4 小时" in chunk.content[0].text

    async def test_missing_tool_call_id_returns_error(self):
        chunk = await _call(tool_call_id="")
        assert "缺少 tool_call_id" in chunk.content[0].text

    async def test_input_schema(self):
        schema = read_tool_result_tool.input_schema
        assert "tool_call_id" in schema["required"]
        assert set(schema["properties"]) == {"tool_call_id", "offset", "limit"}
        assert read_tool_result_tool.is_state_injected is True
        assert read_tool_result_tool.is_read_only is True

    async def test_config_has_read_result_max_output_chars_default(self):
        cfg = ToolResultConfig()
        assert cfg.read_result_max_output_chars == 100_000
        assert cfg.read_result_max_output_chars > cfg.per_tool_threshold_chars
