# -*- coding: utf-8 -*-
"""deerflow formatter 工具调用/结果格式对齐测试（无网络/无 Redis）。

覆盖：文本 chunk type=AIMessageChunk、工具调用流式增量帧
（start 首片带 name/id/index、delta 只带 args 片段，对齐官方
tool_call_chunks 契约）、MODEL_CALL_END 按轮次发 updates 快照、
工具结果 tool 消息帧、非法 args 回退 {}。
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


# ── 工具调用：start/delta 发流式增量帧，end 只标记参数完成 ─────────

def test_tool_call_streaming_chunks(fmt):
    """对齐官方：start 首片带 name/id/index，delta 只带 args 片段。"""
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
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
    # 轮次 id：每轮模型调用一条独立消息（对齐原生每轮新 id）
    assert chunk["id"] == "r1:1"
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
    assert d_chunk["id"] == "r1:1"
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
    assert end_out == []  # end 只标记参数完成，快照由 MODEL_CALL_END 发
    # updates 帧：MODEL_CALL_END 按轮次发完整 ai 消息
    # （jx_chat 前端只读 data.model.messages[0].tool_calls）
    model_end_out = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=10, output_tokens=5),
    )
    assert len(model_end_out) == 2  # 本轮 usage 增量帧 + updates 快照
    # usage 增量帧在前：对齐原生 messages 流最后一块（带 usage）先于
    # 节点写入 state 的快照
    usage_evt = model_end_out[0]
    assert usage_evt.event == EVENT_MESSAGES
    usage_chunk, usage_meta = usage_evt.data
    assert usage_chunk["content"] == ""
    assert usage_chunk["id"] == "r1:1"
    assert usage_chunk["usage_metadata"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert usage_meta == {"langgraph_node": "model"}
    updates_evt = model_end_out[1]
    assert updates_evt.event == EVENT_UPDATES
    ai_msg = updates_evt.data["model"]["messages"][0]
    assert ai_msg["type"] == "ai"
    assert ai_msg["content"] == ""
    # 与流式 chunk 同轮次 id（updates 快照即本轮消息完整形态）
    assert ai_msg["id"] == "r1:1"
    assert ai_msg["tool_calls"] == [
        {"name": "bash", "args": {"command": "ls"}, "id": "c1"},
    ]
    # 不再产出 on_tool_call custom 事件
    assert all(
        e.event != "custom"
        for e in start_out + delta_out + end_out + model_end_out
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
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="{not json", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    out = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=1, output_tokens=1),
    )
    assert out[1].data["model"]["messages"][0]["tool_calls"][0]["args"] == {}


def test_tool_call_non_dict_args_fallback(fmt):
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="[1, 2]", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    out = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=1, output_tokens=1),
    )
    assert out[1].data["model"]["messages"][0]["tool_calls"][0]["args"] == {}


# ── updates 触发时机（MODEL_CALL_END 按轮次组装，对齐原生 model 节点）──

def test_model_call_end_updates_snapshot_groups_round(fmt):
    """一轮一条完整 ai 消息：content + 本轮全部新增 tool_calls + 该轮 usage。"""
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
    fmt.translate(_evt("TEXT_BLOCK_DELTA", delta="先说明", reply_id="r1"))
    fmt.translate(
        _evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="bash", reply_id="r1"),
    )
    fmt.translate(
        _evt("TOOL_CALL_DELTA", tool_call_id="c1", delta='{"command": "ls"}', reply_id="r1"),
    )
    fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    out = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=3, output_tokens=4),
    )
    assert len(out) == 2  # usage 增量帧在前 + updates 快照在后
    ai_msg = out[1].data["model"]["messages"][0]
    assert ai_msg["content"] == "先说明"
    assert ai_msg["id"] == "r1:1"
    assert ai_msg["tool_calls"] == [
        {"name": "bash", "args": {"command": "ls"}, "id": "c1"},
    ]
    # 消息级 usage：该轮单次调用总量（对齐原生 AIMessage.usage_metadata）
    assert ai_msg["usage_metadata"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }
    # 本轮 usage 增量帧同轮次 id、同值
    usage_chunk, _usage_meta = out[0].data
    assert usage_chunk["id"] == "r1:1"
    assert usage_chunk["usage_metadata"] == ai_msg["usage_metadata"]


def test_model_call_end_second_round_excludes_preexisting_calls(fmt):
    """跨轮归属：第二轮快照只含本轮新增调用，不含上一轮旧调用。"""
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c1", tool_call_name="t1", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c1", delta="{}", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c1", reply_id="r1"))
    out1 = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=1, output_tokens=1),
    )
    assert [
        tc["id"] for tc in out1[1].data["model"]["messages"][0]["tool_calls"]
    ] == ["c1"]
    # 工具执行后第二轮
    fmt.translate(_evt("MODEL_CALL_START", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_START", tool_call_id="c2", tool_call_name="t2", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_DELTA", tool_call_id="c2", delta="{}", reply_id="r1"))
    fmt.translate(_evt("TOOL_CALL_END", tool_call_id="c2", reply_id="r1"))
    out2 = fmt.translate(
        _evt("MODEL_CALL_END", reply_id="r1", input_tokens=1, output_tokens=1),
    )
    ai_msg2 = out2[1].data["model"]["messages"][0]
    assert ai_msg2["id"] == "r1:2"
    assert [tc["id"] for tc in ai_msg2["tool_calls"]] == ["c2"]
    # 每轮消息各自挂该轮 usage（不累积上一轮）
    assert ai_msg2["usage_metadata"] == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }
    # 第二轮 usage 增量帧 id 为 r1:2（每轮独立消息）
    usage_chunk2, _ = out2[0].data
    assert usage_chunk2["id"] == "r1:2"
    assert usage_chunk2["usage_metadata"] == ai_msg2["usage_metadata"]


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
    assert len(out) == 2
    evt = out[0]
    assert evt.event == EVENT_MESSAGES
    chunk, metadata = evt.data
    assert chunk["type"] == "tool"
    assert chunk["content"] == "out-1out-2"
    assert chunk["name"] == "bash"
    assert chunk["tool_call_id"] == "c1"
    assert chunk["id"] == "tool:c1"
    assert chunk["status"] == "success"  # 无 state 默认成功（对齐官方）
    assert chunk["artifact"] is None
    assert metadata == {"langgraph_node": "agent"}
    assert evt.event != "custom"
    # updates 帧：对齐原生 tools 节点写入快照
    updates_evt = out[1]
    assert updates_evt.event == EVENT_UPDATES
    tool_msg = updates_evt.data["tools"]["messages"][0]
    assert tool_msg["type"] == "tool"
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == "out-1out-2"


def test_tool_result_state_maps_to_status(fmt):
    """对齐官方 ToolStatus 两值语义：success 之外一律 error。"""
    for state in ("error", "interrupted", "denied"):
        out = fmt.translate(
            _evt(
                "TOOL_RESULT_END",
                tool_call_id="c1",
                reply_id="r1",
                state=state,
            ),
        )
        assert out[0].data[0]["status"] == "error"
