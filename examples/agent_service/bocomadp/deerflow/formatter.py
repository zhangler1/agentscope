# -*- coding: utf-8 -*-
"""AgentEvent → StreamEvent 翻译器。

输入为消息总线上广播/回放的 AgentEvent dict（``model_dump(mode="json")``
产物，``type`` 为大写枚举字符串，payload 含 M2 附加的 ``run_id`` 字段），
输出为 deer-flow 2.0（LangGraph Platform）协议的 :class:`StreamEvent`。

翻译映射（方案决策⑥）：

| AgentEvent（原生）            | StreamEvent            | data 载荷                                   |
|-------------------------------|------------------------|---------------------------------------------|
| ``ReplyStartEvent``           | ``metadata``           | run_id / thread_id / assistant_id 首帧      |
| ``TextBlock*``                | ``messages``           | ``[{"type": "AIMessageChunk", "content", "id"}, metadata]`` |
| ``ThinkingBlock*``            | ``messages``           | chunk 附 ``additional_kwargs.reasoning_content``，metadata 附 ``reasoning: true`` |
| ``ToolCallStart/Delta/End``   | ``messages``           | 流式 ``AIMessageChunk`` 增量帧（``tool_call_chunks`` 首片带 name/id/index、delta 片只带 args，官方前端 SDK 按同 id concat 出 tool_calls）；end 只标记参数完成，完整 ai 消息快照由 MODEL_CALL_END 发 updates 帧 |
| ``ToolResultStart/Text/End``  | ``messages`` + ``updates`` + ``values`` | tool 消息帧（``type=tool`` + ``tool_call_id``）+ ``{"tools": {"messages": [...]}}`` 写入快照；随后 values 快照帧（对齐原生 tools 节点写 state 后快照） |
| ``RequireUserConfirmEvent``   | ``messages`` + ``custom`` + ``values`` | tool 消息帧（``artifact.human_input`` 确认卡片，前端 HumanInputCard 渲染）+ 原 ``on_require_confirm`` + values 快照帧（interrupt 快照含卡片 ToolMessage，对齐原生） |
| ``ModelCallStartEvent``       | （内部消化）             | 记录 model_name，注入 messages 帧 metadata 的 ``ls_model_name`` |
| ``ModelCallEndEvent``         | ``messages`` + ``updates`` + ``values`` | 累积 input/output tokens + 先补发一条本轮 usage 增量帧（chunk 挂 ``usage_metadata``，作为该轮流式最后一块——对齐原生 messages 流最后一块先于节点快照的时序，前端按消息 id 去重累加）；再按轮次发完整 ai 消息快照（``{"model": {"messages": [...]}}``，content + 本轮新增 tool_calls，消息挂该轮 ``usage_metadata``，对齐原生 model 节点写入时机）；随后 values 快照帧（对齐原生 model 节点写 state 后快照，流中每节点边界一帧递增） |
| ``CustomEvent``               | ``custom``             | 原样透传                                   |
| ``ReplyEndEvent(normal)``     | ``end``                | 哨兵（生成器收尾时升级为带 ``usage`` 累计值的 end 帧，对齐原生客户端跨消息累加后的 run 级总量） |
| ``ReplyEndEvent(error)``      | ``error`` + ``end``    | ``{"message", "name"}`` 后接哨兵           |
| 未知事件                      | ``custom``             | 原样透传而非丢弃 |

输入侧按 TEXT/THINKING/TOOL 分支匹配，输出侧统一翻译为 deer-flow 协议。
每个 run 一个实例。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .protocol import (
    END_SENTINEL,
    EVENT_CUSTOM,
    EVENT_END,
    EVENT_ERROR,
    EVENT_MESSAGES,
    EVENT_METADATA,
    EVENT_UPDATES,
    EVENT_VALUES,
    StreamEvent,
)

logger = logging.getLogger(__name__)

# ── 事件类型常量（对齐 agentscope.event.EventType 字符串值）─────────────
_T_REPLY_START = "REPLY_START"
_T_REPLY_END = "REPLY_END"
_T_TEXT_BLOCK_START = "TEXT_BLOCK_START"
_T_TEXT_BLOCK_DELTA = "TEXT_BLOCK_DELTA"
_T_TEXT_BLOCK_END = "TEXT_BLOCK_END"
_T_THINKING_BLOCK_START = "THINKING_BLOCK_START"
_T_THINKING_BLOCK_DELTA = "THINKING_BLOCK_DELTA"
_T_THINKING_BLOCK_END = "THINKING_BLOCK_END"
_T_TOOL_CALL_START = "TOOL_CALL_START"
_T_TOOL_CALL_DELTA = "TOOL_CALL_DELTA"
_T_TOOL_CALL_END = "TOOL_CALL_END"
_T_TOOL_RESULT_START = "TOOL_RESULT_START"
_T_TOOL_RESULT_TEXT_DELTA = "TOOL_RESULT_TEXT_DELTA"
_T_TOOL_RESULT_END = "TOOL_RESULT_END"
_T_REQUIRE_USER_CONFIRM = "REQUIRE_USER_CONFIRM"
_T_CUSTOM = "CUSTOM"
_T_MODEL_CALL_END = "MODEL_CALL_END"

# 确认卡片协议常量（对齐 deer-flow 前端 human_input_request）。
CONFIRM_SOURCE = "agent_scope_permission"
"""确认卡片的 source 标识（前端按 source 区分请求发起方）。"""


def _tool_call_summary(tool_call: dict) -> str:
    """从 tool_call dict 提取面向用户的确认摘要文本。

    优先展示 Bash 的 command / 文件工具的 path，解析失败时回退到原始
    input 字符串（截断 200 字符），保证卡片上总有可读内容。
    """
    name = str(tool_call.get("name", "tool"))
    raw_input = tool_call.get("input", "")
    detail = ""
    if raw_input:
        try:
            parsed = (
                json.loads(raw_input)
                if isinstance(raw_input, str)
                else raw_input
            )
            if isinstance(parsed, dict):
                for key in ("command", "path", "file_path"):
                    if parsed.get(key):
                        detail = str(parsed[key])
                        break
        except Exception:  # noqa: BLE001 —— 摘要尽力而为，失败回退原文
            detail = ""
    if not detail:
        detail = str(raw_input)[:200] if raw_input else "(no arguments)"
    return f"{name}: {detail}"


def build_confirm_card(tool_call: dict) -> dict[str, Any]:
    """构造前端 HumanInputCard 的 tool 消息 chunk。

    输入为 ``RequireUserConfirmEvent.tool_calls`` 中单条 tool_call 的
    ``model_dump(mode="json")`` 产物；输出对齐 deer-flow 前端
    ``extractHumanInputRequest`` 的解析协议（``type=tool`` +
    ``artifact.human_input``）。流式翻译与 threads 端点的刷新恢复
    共用本函数，保证两处卡片结构一致。
    """
    tool_call_id = str(tool_call.get("id", ""))
    request_id = f"confirm-{tool_call_id}"
    question = f"确认执行以下工具调用？\n\n{_tool_call_summary(tool_call)}"
    options = [
        {"id": "option-1", "label": "同意执行", "value": "confirm"},
        {"id": "option-2", "label": "拒绝", "value": "reject"},
    ]
    if tool_call.get("suggested_rules"):
        options.append(
            {
                "id": "option-3",
                "label": "同意并始终允许",
                "value": "confirm_always",
            },
        )
    return {
        "type": "tool",
        "id": request_id,
        # 前端 getMessageGroups 的 isClarificationToolMessage 只认
        # name == "ask_clarification" 的 tool 消息才会渲染 HumanInputCard
        # 组件；其他 name 会落为普通 tool 消息、只显示 content 文本。
        "name": "ask_clarification",
        "tool_call_id": tool_call_id,
        "content": question,
        "artifact": {
            "human_input": {
                "version": 1,
                "kind": "human_input_request",
                "source": CONFIRM_SOURCE,
                "request_id": request_id,
                "tool_call_id": tool_call_id,
                "question": question,
                "input_mode": "single_choice",
                "options": options,
            },
        },
    }


def _evt(event: dict, name: str, data: Any = None) -> StreamEvent:
    """构造一个待补 id 的 StreamEvent（id 由 bridge 填 entry_id）。"""
    return StreamEvent(id="", event=name, data=data)


class DeerflowSSEFormatter:
    """AgentEvent dict → StreamEvent 翻译器（每个 run 一个实例）。"""

    def __init__(self) -> None:
        # metadata 首帧只发一次（reply_start 位置天然对齐 deer-flow 首帧）
        self._metadata_sent = False
        # tool_call_id -> 累积 arguments（delta 片段拼接）
        self._tool_call_args: dict[str, str] = {}
        # tool_call_id -> 工具名（start 时记录，供 delta/end 复用）
        self._tool_call_names: dict[str, str] = {}
        # tool_call_id -> chunk index（并行多工具时按出现顺序分配，
        # 对齐 LangChain tool_call_chunks 的 index 语义）
        self._tool_call_index: dict[str, int] = {}
        # tool_call_id -> 累积 result 文本（tool_result_text_delta 拼接）
        self._tool_result_text: dict[str, str] = {}
        # 本 run 累积 token 用量（MODEL_CALL_END 累加，reply 级聚合；
        # reply 内多轮模型调用合并计数，对齐原生单回复单 AI 消息语义）
        self._usage: dict[str, int] | None = None
        # 当前模型名（MODEL_CALL_START 记录，注入 messages 帧 metadata
        # 的 ``ls_model_name``，对齐原生 LangChain ls_* 透传形态）
        self._model_name: str | None = None
        # 模型调用轮次序号（MODEL_CALL_START 递增；updates 完整 ai 消息
        # 按轮次独立 id，对齐原生每轮模型调用一条消息）
        self._model_call_seq: int = 0
        # 本轮模型调用累积的正文文本（TEXT_BLOCK_DELTA 追加，
        # MODEL_CALL_END 组装完整 ai 消息快照）
        self._model_text: str = ""
        # 本轮模型调用开始前已存在的工具调用 id（区分跨轮新增调用）
        self._model_preexisting_tool_ids: set[str] = set()
        # 本轮结构化消息序列（事件顺序 append：每轮 ai 快照 + tool 消息
        # 交错，对齐原生 state.messages 形态；供 values 收尾快照补入，
        # 使快照含完整 tool_calls/tool 消息而非仅扁平 assistant）
        self._turn_messages: list[dict[str, Any]] = []
        # 本 run 的 reply_id（reply_start 记录，供 values 快照判断
        # storage 尾部扁平 assistant 是否本轮落库）
        self._reply_id: str | None = None
        # agent 名与 thread_id（reply_start 记录，注入 messages 帧
        # metadata 的 ``agent_name``/``thread_id``，对齐原生 LangGraph
        # 运行时透传形态）
        self._agent_name: str | None = None
        self._thread_id: str | None = None

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def translate(self, event: dict) -> list[StreamEvent]:
        """翻译一条 bus 上的 AgentEvent dict。

        返回 0..N 条 StreamEvent；事件 id 为空串，由调用方（bridge）用
        Redis Stream entry_id 填充。未知事件原样透传为 ``custom`` 而非
        丢弃（保证翻译层单点不吞事件）。
        """
        evt_type = str(event.get("type", "")).upper()
        handler = getattr(self, f"_on_{evt_type.lower()}", None)
        if handler is not None:
            try:
                return handler(event)
            except Exception:  # noqa: BLE001 —— 翻译失败不中断流
                logger.exception(
                    "deerflow formatter: failed to translate %s, "
                    "falling back to passthrough",
                    evt_type,
                )
        # 未知事件：原样透传为 custom
        return self._passthrough(event)

    # ------------------------------------------------------------------
    # 各事件翻译（handler 命名 _on_<type.lower()>）
    # ------------------------------------------------------------------

    def _on_reply_start(self, event: dict) -> list[StreamEvent]:
        """ReplyStartEvent → metadata 首帧（run/thread/assistant 标识）。"""
        if self._metadata_sent:
            return []
        self._metadata_sent = True
        self._reply_id = str(event.get("reply_id") or "") or None
        self._agent_name = str(event.get("name") or "") or None
        self._thread_id = str(event.get("session_id") or "") or None
        payload = {
            "run_id": event.get("run_id"),
            "thread_id": event.get("session_id"),
            "assistant_id": event.get("name"),
            "reply_id": event.get("reply_id"),
        }
        return [_evt(event, EVENT_METADATA, payload)]

    def _on_model_call_start(self, event: dict) -> list[StreamEvent]:
        """ModelCallStartEvent → 记录模型名与轮次（内部消化，不产帧）。

        原生 messages 帧 metadata 的 ``ls_model_name`` 来自 LangChain
        模型对象（LangSmith 集成透传）；本适配层无模型对象，从本事件
        （先于一切 token 帧到达）记录模型名，由 :meth:`_model_metadata`
        注入各 model 节点帧。同时递增轮次序号、清空本轮文本缓冲并快照
        既有工具调用集合（供 :meth:`_on_model_call_end` 组装完整 ai
        消息 updates 快照）。不产帧同时避免该事件落入 passthrough 以
        ``model_call_start`` custom 帧泄漏（前端不认识）。
        """
        self._model_name = str(event.get("model_name") or "") or None
        self._model_call_seq += 1
        self._model_text = ""
        self._model_preexisting_tool_ids = set(self._tool_call_args.keys())
        return []

    def _model_metadata(self, langgraph_node: str) -> dict[str, Any]:
        """构造 messages 帧 metadata（对齐原生 LangGraph 运行时透传）。

        ``langgraph_node`` 沿用既有取值（文本/思考帧为 ``agent``、模型
        token 帧与 usage 帧为 ``model``）；``model_name``/``ls_model_name``
        仅在模型名已记录时注入（MODEL_CALL_START 先于一切 token 帧，
        正常流恒有）；``agent_name``/``thread_id`` 自 reply_start 注入
        （deer-flow 业务字段，供前端/观测层按原生键读取）。其余原生
        metadata（langfuse_*、langgraph_step/triggers/path/checkpoint_ns
        等）是 deer-flow LangGraph 运行时/可观测性产物，本适配层无
        等价数据源，如实不注入。
        """
        metadata: dict[str, Any] = {"langgraph_node": langgraph_node}
        if self._model_name:
            metadata["model_name"] = self._model_name
            metadata["ls_model_name"] = self._model_name
        if self._agent_name:
            metadata["agent_name"] = self._agent_name
        if self._thread_id:
            metadata["thread_id"] = self._thread_id
        return metadata

    def _on_reply_end(self, event: dict) -> list[StreamEvent]:
        """ReplyEndEvent → error 帧（失败时）+ end 哨兵。

        usage 不再在此补发——每轮 MODEL_CALL_END 已下发该轮 usage
        增量帧（对齐原生每轮最后一块 chunk 挂 ``usage_metadata``，
        前端按消息 id 去重累加）；run 级累计值由生成器收尾写入 end
        帧 ``data.usage``。
        """
        finished_reason = str(event.get("finished_reason", "")).upper()
        if finished_reason == "ERROR":
            error = event.get("error") or {}
            error_frame = _evt(
                event,
                EVENT_ERROR,
                {
                    "message": error.get("message", "unknown error"),
                    "name": error.get("type", "UNKNOWN"),
                },
            )
            return [error_frame, END_SENTINEL]
        return [END_SENTINEL]

    def _values_frame(self, event: dict) -> StreamEvent:
        """当前 turn 序列的 values 快照帧（节点边界对齐原生每节点快照）。

        原生 ``stream_mode=["values"]`` 在每个 super-step（节点执行完
        写入 state）后都产出一次 state 快照，流中多帧 messages 递增；
        本适配层在节点边界（模型调用完成 / 工具结果完成 / HITL 卡片）
        补发本帧，data 为当前 turn 序列快照，由 SSE 生成器组装成原生
        values 形态（storage 历史 + 本轮序列）后下发。
        """
        return _evt(event, EVENT_VALUES, list(self._turn_messages))

    def _round_msg_id(self, reply_id: Any) -> str:
        """本轮模型调用的消息 id（每轮独立，对齐原生每轮 AIMessage 独立 id）。

        原生 LangChain 每轮模型调用生成一条新的 AIMessageChunk（新 id），
        SDK 按 id concat 出每轮一条消息，前端 ``accumulateUsage`` 按消息
        id 去重累加 usage；本适配层以 ``{reply_id}:{seq}`` 模拟该语义
        （seq 由 MODEL_CALL_START 递增）。无轮次（seq==0，早于
        MODEL_CALL_START 的消息帧）时回退 reply_id。
        """
        if self._model_call_seq:
            return f"{reply_id}:{self._model_call_seq}"
        return str(reply_id or "")

    def _ai_chunk(
        self,
        reply_id: Any,
        content: str = "",
        *,
        reasoning: bool = False,
        tool_call_chunks: list[dict] | None = None,
        usage_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构造 AIMessageChunk 完整序列化形态（对齐原生 LangGraph 帧）。

        原生 messages 帧的 chunk 是 LangChain ``AIMessageChunk.model_dump()``
        全字段形态（用户抓包：content/additional_kwargs/response_metadata/
        type/name/id/tool_calls/invalid_tool_calls/usage_metadata/
        tool_call_chunks/chunk_position）。流式过程中恒空/恒 None 的字段
        也照发（前端 SDK 宽容、对空值不敏感），保证下游按原生键读取
        不落空：

        - ``name``/``chunk_position`` 恒 None（无显式命名/无分块场景）
        - ``tool_calls``/``invalid_tool_calls`` 恒空（流式工具调用走
          ``tool_call_chunks``，与原生流式 chunk 语义一致）
        - ``additional_kwargs``/``response_metadata`` 恒发（thinking 帧
          的 ``additional_kwargs.reasoning_content`` 除外；model_provider
          无数据源，response_metadata 留空 dict）
        - ``usage_metadata`` 仅 usage 帧携带该轮值，其余 None
        """
        chunk: dict[str, Any] = {
            "content": "" if reasoning else content,
            "additional_kwargs": (
                {"reasoning_content": content} if reasoning else {}
            ),
            "response_metadata": {},
            "type": "AIMessageChunk",
            "name": None,
            "id": self._round_msg_id(reply_id),
            "tool_calls": [],
            "invalid_tool_calls": [],
            "usage_metadata": usage_metadata,
            "tool_call_chunks": tool_call_chunks or [],
            "chunk_position": None,
        }
        return chunk

    def _messages_chunk(
        self,
        event: dict,
        content: str,
        *,
        reasoning: bool = False,
    ) -> StreamEvent:
        """构造 messages 增量帧 ``[chunk, metadata]``。

        chunk 对齐 LangGraph 消息 tuple 协议（deer-flow 官方 Python 端
        ``AIMessageChunk.model_dump()`` 序列化，见 :meth:`_ai_chunk`）：

        - ``type`` 取 ``AIMessageChunk``：官方 SDK ``MessageTupleManager.add``
          对其切片归一化为 ``ai``（``endsWith("MessageChunk")`` 检查），
          jx_chat 前端则精确匹配 ``msg.type === "AIMessageChunk"`` 才累加
          文本——两套前端都能消费。
        - ``id`` 必填：无 id 时 SDK 仅 warn 并忽略该 chunk；取本轮轮次 id
          ``{reply_id}:{seq}``（:meth:`_round_msg_id`）——对齐原生每轮
          模型调用一条独立 AIMessage（每轮新 id），SDK 按 id concat 出
          每轮一条消息，同轮内文本/工具/usage 帧聚合进同一消息。
        - thinking 增量进 ``additional_kwargs.reasoning_content``
          （content 留空，避免与正文混淆），LangChain chunk concat 对
          字符串自动拼接，对齐 deer-flow 官方 reasoning 语义。
        """
        chunk = self._ai_chunk(
            event.get("reply_id"),
            content,
            reasoning=reasoning,
        )
        metadata: dict[str, Any] = self._model_metadata("agent")
        if reasoning:
            metadata["reasoning"] = True
        return _evt(event, EVENT_MESSAGES, [chunk, metadata])

    def _on_text_block_start(self, event: dict) -> list[StreamEvent]:
        return [self._messages_chunk(event, "")]

    def _on_text_block_delta(self, event: dict) -> list[StreamEvent]:
        delta = str(event.get("delta", "") or "")
        # 正文累积进本轮缓冲，MODEL_CALL_END 组装完整 ai 消息快照
        # （updates 帧 content），对齐原生 model 节点写入完整消息
        self._model_text += delta
        return [self._messages_chunk(event, delta)]

    def _on_text_block_end(self, event: dict) -> list[StreamEvent]:
        return [self._messages_chunk(event, "")]

    def _on_thinking_block_start(self, event: dict) -> list[StreamEvent]:
        return [self._messages_chunk(event, "", reasoning=True)]

    def _on_thinking_block_delta(self, event: dict) -> list[StreamEvent]:
        return [
            self._messages_chunk(
                event,
                event.get("delta", "") or "",
                reasoning=True,
            ),
        ]

    def _on_thinking_block_end(self, event: dict) -> list[StreamEvent]:
        return [self._messages_chunk(event, "", reasoning=True)]

    # ── 工具调用（arguments 片段跨事件累积）────────────────────────────

    def _on_tool_call_start(self, event: dict) -> list[StreamEvent]:
        """工具调用开始 → 首片 AIMessageChunk 增量帧（官方契约）。

        对齐 deer-flow 官方 messages-tuple 流式形态：``type`` 取
        ``AIMessageChunk``（SDK 切片归一化为 ai 后按同 id concat 合并），
        首片 ``tool_call_chunks`` 携带 name/id/index（args 留空），后续
        delta 帧按 index 累积 args 片段。
        """
        call_id = str(event.get("tool_call_id", ""))
        name = str(event.get("tool_call_name", ""))
        self._tool_call_args[call_id] = ""
        self._tool_call_names[call_id] = name
        if call_id not in self._tool_call_index:
            self._tool_call_index[call_id] = len(self._tool_call_index)
        index = self._tool_call_index[call_id]
        reply_id = event.get("reply_id")
        chunk = self._ai_chunk(
            reply_id,
            tool_call_chunks=[
                {
                    "name": name,
                    "args": "",
                    "id": call_id,
                    "index": index,
                    "type": "tool_call_chunk",
                },
            ],
        )
        return [
            _evt(
                event,
                EVENT_MESSAGES,
                [chunk, self._model_metadata("model")],
            ),
        ]

    def _on_tool_call_delta(self, event: dict) -> list[StreamEvent]:
        """工具调用 args 增量 → AIMessageChunk 增量帧。

        对齐官方形态：delta 帧的 ``tool_call_chunks`` 中 name/id 为 null，
        仅携带 args 片段；SDK concat 时按 index 把 args 逐片拼接。
        """
        call_id = str(event.get("tool_call_id", ""))
        delta = event.get("delta", "") or ""
        self._tool_call_args[call_id] = (
            self._tool_call_args.get(call_id, "") + delta
        )
        if not delta:
            return []
        reply_id = event.get("reply_id")
        index = self._tool_call_index.get(call_id, 0)
        chunk = self._ai_chunk(
            reply_id,
            tool_call_chunks=[
                {
                    "name": None,
                    "args": delta,
                    "id": None,
                    "index": index,
                    "type": "tool_call_chunk",
                },
            ],
        )
        return [
            _evt(
                event,
                EVENT_MESSAGES,
                [chunk, self._model_metadata("model")],
            ),
        ]

    def _on_tool_call_end(self, event: dict) -> list[StreamEvent]:
        """工具调用完成 → 内部消化（参数已在 delta 累积，不产帧）。

        完整 ai 消息（``tool_calls`` 汇总）改由 :meth:`_on_model_call_end`
        在模型调用结束时按轮次组装下发（updates 帧），对齐原生 updates
        通道的 model 节点触发时机（每轮模型调用完成写一条完整消息，
        而非每个工具调用一条）；本事件仅标记参数累积完成。
        """
        return []

    # ── 工具结果（result 文本跨事件累积）───────────────────────────────

    def _on_tool_result_start(self, event: dict) -> list[StreamEvent]:
        call_id = str(event.get("tool_call_id", ""))
        name = str(event.get("tool_call_name", ""))
        self._tool_call_names[call_id] = name
        self._tool_result_text[call_id] = ""
        # 仅在 end 时发 tool 消息帧，start/delta 只累积
        return []

    def _on_tool_result_text_delta(self, event: dict) -> list[StreamEvent]:
        call_id = str(event.get("tool_call_id", ""))
        delta = event.get("delta", "") or ""
        self._tool_result_text[call_id] = (
            self._tool_result_text.get(call_id, "") + delta
        )
        return []

    def _on_tool_result_end(self, event: dict) -> list[StreamEvent]:
        """工具结果完成 → tool 消息 messages 帧 + updates 快照。

        chunk 对齐 deer-flow 官方 ToolMessage 序列化（``type=tool`` +
        ``tool_call_id``）；官方前端 SDK 按 tool_call_id 匹配调用步骤，
        jx_chat 前端按 name 解析搜索溯源 / 追加 ask_clarification 文本。

        对齐原生 updates 通道：工具节点写入快照（``{"tools": {"messages":
        [ToolMessage]}}``，tools 节点键）；本适配层无 stream_mode 协商，
        updates 恒发（jx_chat 只读 model 键、官方 SDK 按 node 名解析，
        tools 键不冲突）。

        values 快照帧随后下发：tool 消息与同轮 ai 快照（含 tool_calls
        + usage_metadata）配对，对齐原生 tools 节点写 state 后的快照
        形态（storage 扁平 history 无工具痕迹的差异由生成器组装时以
        本轮结构化序列兜底补齐）。
        """
        call_id = str(event.get("tool_call_id", ""))
        # 官方 ToolMessage 带 status/artifact；state 取原生
        # ToolResultEndEvent.state（success 之外一律 error，对齐官方
        # ToolStatus 两值语义：error/interrupted/denied → 失败态）。
        state = str(event.get("state", "success"))
        tool_msg: dict[str, Any] = {
            "type": "tool",
            "content": self._tool_result_text.get(call_id, ""),
            "name": self._tool_call_names.get(call_id, ""),
            "tool_call_id": call_id,
            "id": f"tool:{call_id}",  # 独立唯一 id
            "status": (
                "success" if state == "success" else "error"
            ),
            "artifact": None,
            # ToolMessage.model_dump 恒有键（无 reasoning/无 provider 数据
            # 源时为空 dict，对齐原生全字段形态）
            "additional_kwargs": {},
            "response_metadata": {},
        }
        # values 快照序列（本轮结构化消息，对齐原生 state.messages 形态）
        # 追加 tool 消息：原生 values 快照的 ToolMessage 为轻量形态
        # （type/content/name/tool_call_id/id，无流式帧的 status/artifact，
        # 见 client._serialize_message）
        self._turn_messages.append(
            {
                "type": "tool",
                "content": self._tool_result_text.get(call_id, ""),
                "name": self._tool_call_names.get(call_id, ""),
                "tool_call_id": call_id,
                "id": f"tool:{call_id}",
            },
        )
        return [
            _evt(
                event,
                EVENT_MESSAGES,
                [tool_msg, self._model_metadata("agent")],
            ),
            _evt(event, EVENT_UPDATES, {"tools": {"messages": [tool_msg]}}),
            self._values_frame(event),
        ]

    # ── HITL / 自定义 / 兜底 ───────────────────────────────────────────

    def _on_model_call_end(self, event: dict) -> list[StreamEvent]:
        """ModelCallEndEvent → updates 完整 ai 消息快照 + 累积 token。

        对齐原生 updates 通道触发时机：LangGraph 的 model 节点在每轮
        模型调用完成时写入一条完整 AIMessage（content + 本轮新增
        tool_calls）。此前本适配层在 TOOL_CALL_END 时按单个工具调用
        发快照，时机偏早且一条调用一条帧；改为本事件按轮次组装
        （``id=f"{reply_id}:{seq}"``，seq 由 MODEL_CALL_START 递增），
        一轮一条帧、含本轮全部新增调用。

        token 用量同步累积（reply 级聚合，供 end 帧 ``data.usage``）；
        本轮消息快照挂该轮 ``usage_metadata``，并补发一条本轮 usage
        增量帧（chunk 挂 ``usage_metadata``、同轮次 id，作为该轮流式
        最后一块）——对齐原生：LangChain 每轮 AIMessageChunk 的最后
        一块自带该轮单次调用 usage_metadata，SDK concat 进该轮消息，
        前端按消息 id 去重累加出 run 级总量。

        随后补 values 快照帧（当前 turn 序列），对齐原生 model 节点
        写 state 后快照——流中每节点边界一帧、messages 递增，而非
        仅末尾一次全量快照。
        """
        # 1) 累积 token 用量（reply 级聚合）
        input_tokens = int(event.get("input_tokens") or 0)
        output_tokens = int(event.get("output_tokens") or 0)
        if self._usage is None:
            self._usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["total_tokens"] += input_tokens + output_tokens
        # 本轮 usage 增量（消息级：该轮单次调用总量，对齐原生
        # AIMessage.usage_metadata 语义）
        round_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        # 2) 组装本轮完整 ai 消息快照（对齐原生 model 节点写入）
        reply_id = event.get("reply_id")
        new_call_ids = [
            cid
            for cid in self._tool_call_args
            if cid not in self._model_preexisting_tool_ids
        ]
        tool_calls: list[dict[str, Any]] = []
        for cid in new_call_ids:
            args_raw = self._tool_call_args.get(cid, "")
            try:
                args = json.loads(args_raw) if args_raw else {}
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            tool_calls.append(
                {
                    "name": self._tool_call_names.get(cid, ""),
                    "args": args,
                    "id": cid,
                },
            )
        ai_msg: dict[str, Any] = {
            "type": "ai",  # 完整 AIMessage 形态，对齐官方 updates 快照
            "content": self._model_text,
            # 与流式 chunk 同轮次 id：updates 快照即本轮消息的完整形态，
            # 与流式 concat 结果同一条消息（原生 values 快照与 messages
            # 流同 id、客户端去重）；按轮次编号对齐原生每轮一条消息。
            "id": self._round_msg_id(reply_id),
            "tool_calls": tool_calls,
            # 该轮消息级 usage（对齐原生每轮 model 调用消息自带
            # usage_metadata 的形态）
            "usage_metadata": round_usage,
        }
        self._turn_messages.append(ai_msg)
        # 本轮 usage 增量帧：该轮流式最后一块（content 空、只带
        # usage_metadata），SDK 按同 id concat 进本轮消息。置于
        # updates 快照之前——对齐原生时序：messages 流最后一块
        # （带 usage）先于节点写入 state 的快照（values/updates）。
        usage_chunk = self._ai_chunk(
            reply_id,
            usage_metadata=round_usage,
        )
        return [
            _evt(
                event,
                EVENT_MESSAGES,
                [usage_chunk, self._model_metadata("model")],
            ),
            _evt(event, EVENT_UPDATES, {"model": {"messages": [ai_msg]}}),
            # values 快照帧：对齐原生每节点写 state 后快照（流中多帧）
            self._values_frame(event),
        ]

    @property
    def usage(self) -> dict[str, int] | None:
        """本 run 已累积的 token 用量（input/output/total），无调用时 None。

        供 SSE 生成器在 end 前组装 values 快照时附加 ``usage_metadata``。
        """
        return dict(self._usage) if self._usage is not None else None

    @property
    def turn_messages(self) -> list[dict[str, Any]]:
        """本轮结构化消息序列（每轮 ai 快照与 tool 消息按事件顺序交错）。

        对齐原生 values 快照的 state.messages 形态（ai 消息含
        tool_calls/usage_metadata、tool 消息紧随其调用）。SSE 生成器
        在流中每个节点边界帧（:meth:`_values_frame`）与收尾快照组装
        时读取：storage 落库晚于 REPLY_END，本轮结构以此序列为准。
        """
        return list(self._turn_messages)

    @property
    def reply_id(self) -> str | None:
        """本 run 的 reply_id（供 values 快照识别 storage 本轮落库消息）。"""
        return self._reply_id

    def _on_require_user_confirm(self, event: dict) -> list[StreamEvent]:
        """RequireUserConfirmEvent → tool 消息（human_input 卡片）+ custom。

        对齐 deer-flow 前端原生 HITL 协议：每条待确认工具调用翻译为一条
        ``messages`` 帧（``type=tool`` + ``artifact.human_input``），SDK
        将其并入 ``values.messages``，前端 HumanInputCard 自动渲染确认
        卡片（同意/拒绝，携带 suggested_rules 时追加“同意并始终允许”）。

        原 ``on_require_confirm`` custom 事件保留（调试与旧订阅方兼容）。
        """
        stream_events: list[StreamEvent] = []
        tool_calls = event.get("tool_calls") or []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            card = build_confirm_card(tool_call)
            stream_events.append(
                _evt(
                    event,
                    EVENT_MESSAGES,
                    [
                        card,
                        {"langgraph_node": "agent"},
                    ],
                ),
            )
            # values 快照序列同步记录卡片（轻量形态，对齐原生
            # interrupt 时 values 快照含卡片 ToolMessage 的
            # _serialize_message 序列化——无 artifact，前端渲染靠
            # messages 帧的完整卡片）
            self._turn_messages.append(
                {
                    "type": "tool",
                    "content": card["content"],
                    "name": card["name"],
                    "tool_call_id": card["tool_call_id"],
                    "id": card["id"],
                },
            )
        # 原 custom 事件保留（调试/兼容，置于 messages 帧之后）
        stream_events.append(
            _evt(
                event,
                EVENT_CUSTOM,
                {
                    "type": "on_require_confirm",
                    "reply_id": event.get("reply_id"),
                    "tool_calls": tool_calls,
                },
            ),
        )
        # values 快照帧：interrupt 快照含卡片 ToolMessage（对齐原生
        # 等待确认时的 state 快照，前端刷新后仍能恢复卡片渲染）
        stream_events.append(self._values_frame(event))
        # HITL park 收尾：原生在等待确认时不会发出 ReplyEndEvent（reply
        # 尚未结束，等待 Case B 续跑），bridge 的 live 订阅永远等不到 end
        # 哨兵，SSE 连接只靠心跳帧空转不关闭，前端 ``isStreaming`` 一直
        # 卡死（确认卡片也因此 disabled）。此处补发 end 哨兵让本轮流在
        # 卡片帧后正常收尾，用户点击确认/拒绝后由前端发起新 run 续跑。
        stream_events.append(END_SENTINEL)
        return stream_events

    def _on_custom(self, event: dict) -> list[StreamEvent]:
        """CustomEvent → custom 原样透传（type 取事件 name）。"""
        value = dict(event.get("value") or {})
        value["type"] = event.get("name", "custom")
        return [_evt(event, EVENT_CUSTOM, value)]

    def _passthrough(self, event: dict) -> list[StreamEvent]:
        """未知事件：custom 原样透传（剥掉内部元字段）。"""
        logger.debug(
            "deerflow formatter: passthrough unhandled type=%s",
            event.get("type"),
        )
        payload = {
            k: v
            for k, v in event.items()
            if k not in ("type", "run_id", "_entry_id")
        }
        payload.setdefault("type", str(event.get("type", "unknown")).lower())
        return [_evt(event, EVENT_CUSTOM, payload)]


__all__ = ["DeerflowSSEFormatter", "build_confirm_card"]
