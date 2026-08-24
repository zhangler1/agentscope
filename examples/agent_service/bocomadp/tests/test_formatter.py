# -*- coding: utf-8 -*-
"""deerflow formatter 工具调用/结果格式对齐测试（无网络/无 Redis）。

覆盖：文本 chunk type=AIMessageChunk、工具调用流式增量帧
（start 首片带 name/id/index、delta 只带 args 片段，对齐官方
tool_call_chunks 契约）、end 发 updates 快照、工具结果 tool 消息帧、
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


# ── 工具调用：start/delta 发流式增量帧，end 发 updates 快照 ───────────

def test_tool_call_streaming_chunks(fmt):
    """对齐官方：start 首片带 name/id/index，delta 只带 args 片段。"""
    start_out = fmt.translate(
        _evt(
            "TOOL_CALL_START",
            tool_call_id="c1",
            tool_call_name="bash",
            reply_id="r1",
        ),
    )
    assert len(start_out) == 1
    assert start_out[0].event == EVENT_MESSAGES
    chunk, metadata = start_out[0].data
    assert chunk["type"] == "AIMessageChunk"
    assert chunk["content"] == ""
    assert chunk["id"] == "r1"  # 与文本 chunk 同 id：SDK concat 合并
    assert chunk["tool_call_chunks"] == [
        {
            "name": "bash",
            "args": "",
            "id": "c1",
            "index": 0,
            "type": "tool_call_chunk",
        },
    ]
    assert metadata == {"langgraph_node": "model"}

    delta_out = fmt.translate(
        _evt(
            "TOOL_CALL_DELTA",
            tool_call_id="c1",
            delta='{"command": "ls"}',
            reply_id="r1",
        ),
    )
    assert len(delta_out) == 1
    d_chunk, d_metadata = delta_out[0].data
    assert d_chunk["type"] == "AIMessageChunk"
    assert d_chunk["id"] == "r1"
    # delta 片：name/id 为 None，只带 args 片段（官方形态）
    assert d_chunk["tool_call_chunks"] == [
        {
            "name": None,
            "args": '{"command": "ls"}',
            "id": None,
            "index": 0,
            "type": "tool_call_chunk",
        },
    ]
    assert d_metadata == {"langgraph_node": "model"}

    end_out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert len(end_out) == 1
    updates_evt = end_out[0]
    # updates 帧：jx_chat 前端只读 data.model.messages[0]
    assert updates_evt.event == EVENT_UPDATES
    ai_msg = updates_evt.data["model"]["messages"][0]
    assert ai_msg["type"] == "ai"
    assert ai_msg["content"] == ""
    assert ai_msg["id"] == "r1:tool:c1"  # 独立 id，不与流式 chunk 混淆
    assert ai_msg["tool_calls"] == [
        {"name": "bash", "args": {"command": "ls"}, "id": "c1"},
    ]
    # 不再产出 on_tool_call custom 事件
    assert all(
        e.event != "custom" for e in start_out + delta_out + end_out
    )


def test_tool_call_empty_delta_emits_nothing(fmt):
    """空 delta 不产出帧（避免无内容增量帧干扰 SDK concat）。"""
    fmt.translate(
        _evt(
            "TOOL_CALL_START",
            tool_call_id="c1",
            tool_call_name="bash",
            reply_id="r1",
        ),
    )
    assert fmt.translate(
        _evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="", reply_id="r1"),
    ) == []


def test_tool_call_parallel_index_allocation(fmt):
    """并行多工具：chunk index 按 start 出现顺序分配（0/1/2...）。"""
    out_a = fmt.translate(
        _evt("TOOL_CALL_START", tool_call_id="a", tool_call_name="t1", reply_id="r1"),
    )
    out_b = fmt.translate(
        _evt("TOOL_CALL_START", tool_call_id="b", tool_call_name="t2", reply_id="r1"),
    )
    assert out_a[0].data[0]["tool_call_chunks"][0]["index"] == 0
    assert out_b[0].data[0]["tool_call_chunks"][0]["index"] == 1
    # 交错 delta 分别落到各自 index
    d_a = fmt.translate(
        _evt("TOOL_CALL_DELTA", tool_call_id="a", delta="{}", reply_id="r1"),
    )
    d_b = fmt.translate(
        _evt("TOOL_CALL_DELTA", tool_call_id="b", delta="{}", reply_id="r1"),
    )
    assert d_a[0].data[0]["tool_call_chunks"][0]["index"] == 0
    assert d_b[0].data[0]["tool_call_chunks"][0]["index"] == 1


def test_tool_call_invalid_json_args_fallback(fmt):
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="{not json", reply_id="r1"))
    out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert out[0].data["model"]["messages"][0]["tool_calls"][0]["args"] == {}


def test_tool_call_non_dict_args_fallback(fmt):
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="[1, 2]", reply_id="r1"))
    out = fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    assert out[0].data["model"]["messages"][0]["tool_calls"][0]["args"] == {}


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
