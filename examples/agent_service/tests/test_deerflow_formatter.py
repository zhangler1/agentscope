"""DeerflowSSEFormatter 事件翻译单测（formatter.py）。

覆盖方案决策⑥映射表全部六类映射 + 未知事件透传兜底。输入为总线上的
AgentEvent dict（``model_dump(mode="json")`` 形态，type 为大写枚举串）。
"""

from __future__ import annotations

from bocomadp.deerflow.formatter import DeerflowSSEFormatter
from bocomadp.deerflow.protocol import (
    END_SENTINEL,
    EVENT_CUSTOM,
    EVENT_ERROR,
    EVENT_MESSAGES,
    EVENT_METADATA,
    EVENT_UPDATES,
    EVENT_VALUES,
)


def _reply_start(run_id: str = "run1", session_id: str = "t1") -> dict:
    return {
        "type": "REPLY_START",
        "session_id": session_id,
        "reply_id": "r1",
        "name": "agent_a",
        "role": "assistant",
        "run_id": run_id,
    }


def _reply_end(finished_reason: str = "COMPLETED", error: dict | None = None) -> dict:
    return {
        "type": "REPLY_END",
        "session_id": "t1",
        "reply_id": "r1",
        "finished_reason": finished_reason,
        "error": error,
        "run_id": "run1",
    }


# ── metadata 首帧 ─────────────────────────────────────────────────────


def test_reply_start_emits_metadata_once() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate(_reply_start())
    assert len(evts) == 1
    assert evts[0].event == EVENT_METADATA
    assert evts[0].data["run_id"] == "run1"
    assert evts[0].data["thread_id"] == "t1"
    assert evts[0].data["assistant_id"] == "agent_a"
    assert evts[0].data["reply_id"] == "r1"
    # 后续事件不再重复 metadata
    assert f.translate(_reply_start()) == []


# ── messages 增量 ─────────────────────────────────────────────────────


def test_text_delta_maps_to_messages() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate(
        {"type": "TEXT_BLOCK_DELTA", "reply_id": "r1", "block_id": "b1", "delta": "你好", "run_id": "run1"},
    )
    assert len(evts) == 1
    assert evts[0].event == EVENT_MESSAGES
    # chunk 对齐 LangGraph 消息 tuple 协议：type/id 必填（langgraph-sdk
    # MessageTupleManager 缺 type 崩溃、缺 id 忽略），id 用 reply_id 聚合；
    # 同时对齐原生 LangChain AIMessageChunk.model_dump 全字段形态——
    # 流式过程中恒空/恒 None 的字段（tool_calls/invalid_tool_calls/
    # usage_metadata/chunk_position/name）照发，下游按原生键读取不落空
    chunk, metadata = evts[0].data
    assert chunk == {
        "content": "你好",
        "additional_kwargs": {},
        "response_metadata": {},
        "type": "AIMessageChunk",
        "name": None,
        "id": "r1",
        "tool_calls": [],
        "invalid_tool_calls": [],
        "usage_metadata": None,
        "tool_call_chunks": [],
        "chunk_position": None,
    }
    assert metadata == {"langgraph_node": "agent"}


def test_text_block_start_end_map_to_messages() -> None:
    f = DeerflowSSEFormatter()
    start = f.translate({"type": "TEXT_BLOCK_START", "reply_id": "r1", "block_id": "b1", "run_id": "run1"})
    end = f.translate({"type": "TEXT_BLOCK_END", "reply_id": "r1", "block_id": "b1", "run_id": "run1"})
    assert start[0].event == EVENT_MESSAGES and start[0].data[0]["content"] == ""
    assert end[0].event == EVENT_MESSAGES and end[0].data[0]["content"] == ""
    assert start[0].data[0]["type"] == "AIMessageChunk" and start[0].data[0]["id"] == "r1"
    assert end[0].data[0]["type"] == "AIMessageChunk" and end[0].data[0]["id"] == "r1"


def test_thinking_delta_carries_reasoning_flag() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate(
        {"type": "THINKING_BLOCK_DELTA", "reply_id": "r1", "block_id": "b1", "delta": "推理中", "run_id": "run1"},
    )
    assert evts[0].event == EVENT_MESSAGES
    assert evts[0].data[1] == {"langgraph_node": "agent", "reasoning": True}
    # thinking 增量进 additional_kwargs.reasoning_content（对齐 deer-flow
    # 官方 patched deepseek/mimo 语义），content 留空避免与正文混淆
    assert evts[0].data[0]["type"] == "AIMessageChunk"
    assert evts[0].data[0]["content"] == ""
    assert evts[0].data[0]["id"] == "r1"
    assert evts[0].data[0]["additional_kwargs"] == {"reasoning_content": "推理中"}


# ── custom：工具调用（arguments 跨事件累积）──────────────────────────


def test_tool_call_accumulates_arguments() -> None:
    """工具调用：start 首片带 name/id/index，delta 只带 args 片段，
    end 只标记参数完成，MODEL_CALL_END 发本轮 usage 增量帧（该轮流式
    最后一块，先于快照）+ updates 快照（完整 tool_calls，jx_chat 前端
    消费）。"""
    f = DeerflowSSEFormatter()
    f.translate(
        {"type": "MODEL_CALL_START", "reply_id": "r1", "run_id": "run1"},
    )
    start = f.translate(
        {"type": "TOOL_CALL_START", "reply_id": "r1", "tool_call_id": "c1", "tool_call_name": "get_balance", "run_id": "run1"},
    )
    assert start[0].event == EVENT_MESSAGES
    chunk, metadata = start[0].data
    assert chunk["type"] == "AIMessageChunk"
    # 轮次 id：每轮模型调用一条独立消息（对齐原生每轮新 id）
    assert chunk["id"] == "r1:1"
    assert chunk["tool_call_chunks"] == [
        {"name": "get_balance", "args": "", "id": "c1", "index": 0, "type": "tool_call_chunk"},
    ]
    assert metadata == {"langgraph_node": "model"}

    delta = f.translate(
        {"type": "TOOL_CALL_DELTA", "reply_id": "r1", "tool_call_id": "c1", "delta": '{"ac', "run_id": "run1"},
    )
    assert delta[0].data[0]["tool_call_chunks"] == [
        {"name": None, "args": '{"ac', "id": None, "index": 0, "type": "tool_call_chunk"},
    ]

    end = f.translate({"type": "TOOL_CALL_END", "reply_id": "r1", "tool_call_id": "c1", "run_id": "run1"})
    assert end == []  # 参数在 delta 累积，快照由 MODEL_CALL_END 发
    model_end = f.translate(
        {"type": "MODEL_CALL_END", "reply_id": "r1", "input_tokens": 10, "output_tokens": 5, "run_id": "run1"},
    )
    assert len(model_end) == 3
    # usage 增量帧在前：对齐原生 messages 流最后一块先于节点快照
    assert model_end[0].event == EVENT_MESSAGES
    usage_chunk, usage_meta = model_end[0].data
    assert usage_chunk["type"] == "AIMessageChunk"
    assert usage_chunk["content"] == ""
    assert usage_chunk["id"] == "r1:1"
    assert usage_chunk["usage_metadata"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert usage_meta == {"langgraph_node": "model"}
    assert model_end[1].event == "updates"
    ai_msg = model_end[1].data["model"]["messages"][0]
    assert ai_msg["tool_calls"][0]["args"] == {}  # 非法 args 片段回退 {}
    # 快照消息挂该轮 usage_metadata（对齐原生消息级 usage）
    assert ai_msg["usage_metadata"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    # 尾帧为 values 快照帧（model 节点边界，对齐原生每节点快照）
    assert model_end[2].event == EVENT_VALUES
    assert model_end[2].data == [ai_msg]


# ── custom：工具结果（result 文本跨事件累积）─────────────────────────


def test_tool_result_accumulates_text() -> None:
    """工具结果：start/delta 只累积不产帧，end 发 tool 消息帧。"""
    f = DeerflowSSEFormatter()
    start = f.translate(
        {"type": "TOOL_RESULT_START", "reply_id": "r1", "tool_call_id": "c1", "tool_call_name": "get_balance", "run_id": "run1"},
    )
    assert start == []

    d1 = f.translate(
        {"type": "TOOL_RESULT_TEXT_DELTA", "reply_id": "r1", "tool_call_id": "c1", "delta": "余额", "run_id": "run1"},
    )
    d2 = f.translate(
        {"type": "TOOL_RESULT_TEXT_DELTA", "reply_id": "r1", "tool_call_id": "c1", "delta": "100 元", "run_id": "run1"},
    )
    assert d1 == [] and d2 == []

    end = f.translate({"type": "TOOL_RESULT_END", "reply_id": "r1", "tool_call_id": "c1", "state": "success", "run_id": "run1"})
    assert end[0].event == EVENT_MESSAGES
    chunk, metadata = end[0].data
    assert chunk["type"] == "tool"
    assert chunk["content"] == "余额100 元"
    assert chunk["name"] == "get_balance"
    assert chunk["tool_call_id"] == "c1"
    assert chunk["status"] == "success"
    assert metadata == {"langgraph_node": "agent"}
    # values 快照序列同步记录 tool 消息（轻量形态，对齐原生
    # client._serialize_message 的 ToolMessage 序列化）
    assert f.turn_messages == [
        {
            "type": "tool",
            "content": "余额100 元",
            "name": "get_balance",
            "tool_call_id": "c1",
            "id": "tool:c1",
        },
    ]
    # 尾帧为 values 快照帧（tools 节点边界，对齐原生每节点快照）
    assert end[1].event == EVENT_UPDATES
    assert end[2].event == EVENT_VALUES
    assert end[2].data == f.turn_messages


# ── custom：HITL / 自定义事件 ────────────────────────────────────────


def test_require_user_confirm_maps_to_custom() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate(
        {
            "type": "REQUIRE_USER_CONFIRM",
            "reply_id": "r1",
            "tool_calls": [{"id": "c1", "name": "get_balance"}],
            "run_id": "run1",
        },
    )
    # 前端不认识 custom on_require_confirm，但 custom 事件保留；
    # custom 后补 values 快照帧（interrupt 快照含卡片，对齐原生），
    # 尾部 end 哨兵使本轮 SSE 在卡片帧后收尾（park 无 ReplyEndEvent，
    # 否则连接永不关闭、前端 isStreaming 卡死）
    custom = evts[-3]
    assert evts[-1] is END_SENTINEL
    assert evts[-2].event == EVENT_VALUES
    assert evts[-2].data == [
        {
            "type": "tool",
            "content": "确认执行以下工具调用？\n\nget_balance: (no arguments)",
            "name": "ask_clarification",
            "tool_call_id": "c1",
            "id": "confirm-c1",
        },
    ]
    assert custom.event == EVENT_CUSTOM
    assert custom.data["type"] == "on_require_confirm"
    assert custom.data["reply_id"] == "r1"
    assert custom.data["tool_calls"] == [{"id": "c1", "name": "get_balance"}]


def test_require_user_confirm_emits_human_input_card() -> None:
    """RequireUserConfirmEvent → tool 消息帧（human_input 确认卡片）。

    前端 SDK 将 messages 帧并入 values.messages，HumanInputCard 据此
    渲染确认卡片；此测试验证 chunk 结构与 artifact.human_input 载荷
    可被前端 parseHumanInputRequest 解析。
    """
    f = DeerflowSSEFormatter()
    evts = f.translate(
        {
            "type": "REQUIRE_USER_CONFIRM",
            "reply_id": "r1",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "Bash",
                    "input": '{"command": "mkdir -p /tmp/demo", "description": "建目录"}',
                    "state": "asking",
                },
            ],
            "run_id": "run1",
        },
    )
    # 首条为 messages 帧（tool 消息），次为 custom 事件，尾部 end 哨兵
    msg_evt = evts[0]
    assert msg_evt.event == EVENT_MESSAGES
    chunk, metadata = msg_evt.data
    assert metadata["langgraph_node"] == "agent"
    assert chunk["type"] == "tool"
    # 前端 isClarificationToolMessage 只认该 name 才会渲染确认卡片
    assert chunk["name"] == "ask_clarification"
    assert chunk["tool_call_id"] == "c1"
    assert chunk["id"] == "confirm-c1"
    human_input = chunk["artifact"]["human_input"]
    assert human_input["kind"] == "human_input_request"
    assert human_input["source"] == "agent_scope_permission"
    assert human_input["request_id"] == "confirm-c1"
    assert human_input["input_mode"] == "single_choice"
    assert "mkdir -p /tmp/demo" in human_input["question"]
    values = [o["value"] for o in human_input["options"]]
    assert values == ["confirm", "reject"]
    # 帧序：messages 卡片帧 → custom → values 快照帧 → end 哨兵
    assert len(evts) == 4
    assert evts[-2].event == EVENT_VALUES
    assert evts[-1] is END_SENTINEL


def test_require_user_confirm_adds_always_allow_option() -> None:
    """携带 suggested_rules 时追加“同意并始终允许”选项。"""
    f = DeerflowSSEFormatter()
    evts = f.translate(
        {
            "type": "REQUIRE_USER_CONFIRM",
            "reply_id": "r1",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "Bash",
                    "input": "{\"command\": \"mkdir -p /tmp/demo\"}",
                    "state": "asking",
                    "suggested_rules": [
                        {
                            "tool_name": "Bash",
                            "rule_content": "mkdir:*",
                            "behavior": "allow",
                            "source": "suggested",
                        },
                    ],
                },
            ],
            "run_id": "run1",
        },
    )
    msg_evt = evts[0]
    chunk, _metadata = msg_evt.data
    human_input = chunk["artifact"]["human_input"]
    values = [o["value"] for o in human_input["options"]]
    assert values == ["confirm", "reject", "confirm_always"]
    assert evts[-1] is END_SENTINEL


def test_custom_event_passthrough() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate({"type": "CUSTOM", "name": "state_updated", "value": {"k": 1}, "run_id": "run1"})
    assert evts[0].event == EVENT_CUSTOM
    assert evts[0].data == {"k": 1, "type": "state_updated"}


# ── end / error ───────────────────────────────────────────────────────


def test_reply_end_normal_emits_end_sentinel() -> None:
    f = DeerflowSSEFormatter()
    assert f.translate(_reply_end()) == [END_SENTINEL]

def test_reply_end_error_emits_error_then_end() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate(
        _reply_end(finished_reason="ERROR", error={"type": "MODEL_ERROR", "message": "boom"}),
    )
    assert len(evts) == 2
    assert evts[0].event == EVENT_ERROR
    assert evts[0].data == {"message": "boom", "name": "MODEL_ERROR"}
    assert evts[1] is END_SENTINEL


def test_reply_end_interrupted_emits_end_only() -> None:
    """cancel 后的 REPLY_END(INTERRUPTED) 只收敛为 end（不带 error）。"""
    f = DeerflowSSEFormatter()
    assert f.translate(_reply_end(finished_reason="INTERRUPTED")) == [END_SENTINEL]


# ── token 用量（MODEL_CALL_END 累积 + 每轮 usage 帧，end 帧带累计）──


def _model_call_end(input_tokens: int, output_tokens: int) -> dict:
    return {
        "type": "MODEL_CALL_END",
        "session_id": "t1",
        "reply_id": "r1",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "finished_reason": "stop",
        "run_id": "run1",
    }


def test_model_call_end_emits_updates_snapshot_and_usage() -> None:
    """MODEL_CALL_END 发本轮 usage 增量帧（先于快照，对齐原生 messages
    流最后一块先于节点写入 state）+ updates 完整 ai 消息快照（挂该轮
    usage_metadata），并累积 reply 级 usage（``f.usage`` 只读视图）。"""
    f = DeerflowSSEFormatter()
    f.translate(
        {"type": "MODEL_CALL_START", "reply_id": "r1", "run_id": "run1"},
    )
    out = f.translate(_model_call_end(120, 45))
    assert len(out) == 3
    # 本轮 usage 增量帧（该轮流式最后一块，同轮次 id，先于快照）
    assert out[0].event == EVENT_MESSAGES
    usage_chunk, usage_meta = out[0].data
    assert usage_chunk["usage_metadata"] == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }
    assert usage_chunk["id"] == "r1:1"
    assert usage_meta == {"langgraph_node": "model"}
    assert out[1].event == "updates"
    ai_msg = out[1].data["model"]["messages"][0]
    assert ai_msg["type"] == "ai"
    assert ai_msg["content"] == ""
    assert ai_msg["tool_calls"] == []  # 无工具调用轮次：空列表
    assert ai_msg["usage_metadata"] == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }
    # values 快照序列同步记录 ai 快照（无 tool 消息时仅一条）
    assert f.turn_messages == [ai_msg]
    # 尾帧为 values 快照帧（model 节点边界，对齐原生每节点快照）
    assert out[2].event == EVENT_VALUES
    assert out[2].data == [ai_msg]
    assert f.usage == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }


def test_model_call_end_accumulates_across_calls() -> None:
    """多轮模型调用按 reply 聚合（对齐原生单回复单 AI 消息语义），
    每轮快照消息的 usage_metadata 各自为本轮值（不累积）。"""
    f = DeerflowSSEFormatter()
    f.translate(_model_call_end(100, 10))
    out2 = f.translate(_model_call_end(20, 35))
    assert f.usage == {
        "input_tokens": 120,
        "output_tokens": 45,
        "total_tokens": 165,
    }
    ai_msg2 = out2[1].data["model"]["messages"][0]
    assert ai_msg2["usage_metadata"] == {
        "input_tokens": 20,
        "output_tokens": 35,
        "total_tokens": 55,
    }


def test_reply_end_emits_end_sentinel_only() -> None:
    """正常结束只发 end 哨兵：usage 已在每轮 MODEL_CALL_END 下发
    （每轮一条 usage 帧），无末尾 run 级累计帧（原生形态）。"""
    f = DeerflowSSEFormatter()
    f.translate(_model_call_end(120, 45))
    assert f.translate(_reply_end()) == [END_SENTINEL]


def test_reply_end_error_emits_error_only() -> None:
    """error 流只发 error + end（usage 随每轮 MODEL_CALL_END 已下发）。"""
    f = DeerflowSSEFormatter()
    f.translate(_model_call_end(120, 45))
    evts = f.translate(
        _reply_end(finished_reason="ERROR", error={"type": "MODEL_ERROR", "message": "boom"}),
    )
    assert len(evts) == 2
    assert evts[0].event == EVENT_ERROR
    assert evts[1] is END_SENTINEL


# ── 未知事件兜底 ─────────────────────────────────────────────────────


def test_unknown_event_passthrough_as_custom() -> None:
    f = DeerflowSSEFormatter()
    evts = f.translate({"type": "SOME_FUTURE_EVENT", "foo": "bar", "run_id": "run1"})
    assert len(evts) == 1
    assert evts[0].event == EVENT_CUSTOM
    assert evts[0].data["type"] == "some_future_event"
    assert evts[0].data["foo"] == "bar"
    # 内部元字段不外泄
    assert "run_id" not in evts[0].data
