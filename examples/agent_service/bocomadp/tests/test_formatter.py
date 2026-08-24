# -*- coding: utf-8 -*-
"""deerflow formatter 工具调用/结果格式对齐测试（无网络/无 Redis）。

覆盖：文本 chunk type=AIMessageChunk、工具调用 end 双发
messages + updates 帧、工具结果 tool 消息帧、start/delta 不产出、
非法 args 回退 {}。
"""
from __future__ import annotations

import pytest

from bocomadp.deerflow.formatter import DeerflowSSEFormatter
from bocomadp.deerflow.protocol import EVENT_MESSAGES, EVENT_UPDATES


def _evt(evt_type: str, **payload) -> dict:
    d: dict = {"type": evt_type}
    d.update(payload)
    return d


@pytest.fixture
def fmt() -> DeerflowSSEFormatter:
    return DeerflowSSEFormatter()


# ── 文本增量：chunk type 必须为 AIMessageChunk ──────────────────────────

def test_text_block_delta_chunk_type(fmt):
    out = fmt.translate(_evt("TEXT_BLOCK_DELTA", delta="你好", reply_id="r1"))
    assert len(out) == 1
    assert out[0].event == EVENT_MESSAGES
    chunk, metadata = out[0].data
    assert chunk["type"] == "AIMessageChunk"
    assert chunk["content"] == "你好"
    assert chunk["id"] == "r1"
    assert metadata == {"langgraph_node": "agent"}


# ── 工具调用：start/delta 不产出，end 双发 messages + updates ───────────

def test_tool_call_only_emits_on_end(fmt):
    assert fmt.translate(
        _evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"),
    ) == []
    assert fmt.translate(
        _evt("TOOL_CALL_DELTA", tool_call_id="c1", delta='{"command": "ls"}', reply_id="r1"),
    ) == []
    out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert len(out) == 2
    msgs_evt, updates_evt = out
    # messages 帧：官方 [chunk, metadata] 元组
    assert msgs_evt.event == EVENT_MESSAGES
    chunk, metadata = msgs_evt.data
    assert chunk["type"] == "ai"
    assert chunk["content"] == ""
    assert chunk["id"] == "r1:tool:c1"  # 独立于 reply_id，避免 SDK concat 丢 tool_calls
    assert chunk["tool_calls"] == [
        {"name": "bash", "args": {"command": "ls"}, "id": "c1"},
    ]
    assert metadata == {"langgraph_node": "agent"}
    # updates 帧：jx_chat 前端只读 data.model.messages[0]
    assert updates_evt.event == EVENT_UPDATES
    assert updates_evt.data == {"model": {"messages": [chunk]}}
    # 不再产出 on_tool_call custom 事件
    assert all(e.event != "custom" for e in out)


def test_tool_call_invalid_json_args_fallback(fmt):
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="{not json", reply_id="r1"))
    out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert out[0].data[0]["tool_calls"][0]["args"] == {}


def test_tool_call_non_dict_args_fallback(fmt):
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="[1, 2]", reply_id="r1"))
    out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert out[0].data[0]["tool_calls"][0]["args"] == {}


# ── 工具结果：start/delta 不产出，end 发 tool 消息帧 ────────────────────

def test_tool_result_only_emits_on_end(fmt):
    assert fmt.translate(
        _evt("TOOL_RESULT_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"),
    ) == []
    assert fmt.translate(
        _evt("TOOL_RESULT_TEXT_DELTA", tool_call_id="c1", delta="out-1", reply_id="r1"),
    ) == []
    assert fmt.translate(
        _evt("TOOL_RESULT_TEXT_DELTA", tool_call_id="c1", delta="out-2", reply_id="r1"),
    ) == []
    out = fmt.translate(_evt("TOOL_RESULT_END", tool_call_id="c1", reply_id="r1"))
    assert len(out) == 1
    evt = out[0]
    assert evt.event == EVENT_MESSAGES
    chunk, metadata = evt.data
    assert chunk["type"] == "tool"
    assert chunk["content"] == "out-1out-2"
    assert chunk["name"] == "bash"
    assert chunk["tool_call_id"] == "c1"
    assert chunk["id"] == "tool:c1"
    assert metadata == {"langgraph_node": "agent"}
    assert evt.event != "custom"
