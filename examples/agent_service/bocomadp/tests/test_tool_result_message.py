# -*- coding: utf-8 -*-
"""预览消息与内容提取测试。"""
from __future__ import annotations

from agentscope.message import Base64Source, DataBlock, TextBlock

from bocomadp import tool_result_message as trm


def test_extract_text_from_string():
    assert trm.extract_text_content("plain") == "plain"


def test_extract_text_from_blocks():
    blocks = [TextBlock(text="a"), TextBlock(text="b")]
    assert trm.extract_text_content(blocks) == "ab"


def test_extract_text_returns_none_for_data_block():
    blocks = [
        TextBlock(text="a"),
        DataBlock(name="img", source=Base64Source(data="", media_type="image/png")),
    ]
    assert trm.extract_text_content(blocks) is None


def test_generate_preview_small_content():
    preview, has_more = trm.generate_preview("abc", 2000)
    assert preview == "abc" and has_more is False


def test_generate_preview_cuts_at_newline():
    content = "x" * 700 + "\n" + "y" * 700 + "\n" + "z" * 700
    preview, has_more = trm.generate_preview(content, 1000)
    assert has_more is True
    # 1000 窗口内最后一个换行位于 700(>500),应断在换行处,避免切在行中间
    assert preview == "x" * 700


def test_build_persisted_message_contains_key_and_tags():
    msg = trm.build_persisted_message(
        "tool_result:s:t", 12345, "hello world", 2000, tool_call_id="t",
    )
    assert msg.startswith(trm.PERSISTED_OUTPUT_TAG)
    assert msg.endswith(trm.PERSISTED_OUTPUT_CLOSING_TAG)
    assert "tool_result:s:t" in msg
    assert "read_tool_result" in msg  # 提示模型用读回工具
    assert '"t"' in msg  # 读回提示中显式给出 tool_call_id,模型据此调参


def test_build_persisted_message_without_tool_call_id():
    msg = trm.build_persisted_message("tool_result:s:t", 12345, "hello", 2000)
    assert 'tool_call_id = ""' in msg


def test_build_persisted_message_mentions_max_output_chars():
    """提示词告知模型单次读回上限(来自系统配置 read_result_max_output_chars)。"""
    msg = trm.build_persisted_message(
        "tool_result:s:t", 12345, "hello", 2000,
        tool_call_id="t", max_output_chars=50000,
    )
    assert "单次最多可读取 50000 字符" in msg
    assert "limit 设为 50000 以内" in msg


def test_format_size():
    assert trm.format_size(500) == "500B"
    assert trm.format_size(2048) == "2.0KB"
    assert trm.format_size(3 * 1024 * 1024) == "3.0MB"
