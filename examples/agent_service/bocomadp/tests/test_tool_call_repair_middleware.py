# -*- coding: utf-8 -*-
"""ToolCallRepairMiddleware 测试：发送前 tool_call 参数的体检/自愈/成对剔除。"""
from __future__ import annotations

import json
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock

from bocomadp.middleware import tool_call_repair as mod
from bocomadp.middleware.tool_call_repair import (
    ToolCallRepairMiddleware,
    repair_tool_call_input,
    tool_call_repair_enabled,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
_FULL_CONTENT = "---\nname: invoice-extractor\n" + "发票字段校验规则。\n" * 40
_FULL_ARGS = json.dumps(
    {
        "file_path": "/workspace/shared/skills/invoice-extractor/SKILL.md",
        "content": _FULL_CONTENT,
    },
    ensure_ascii=False,
)
# 复刻线上形态：max_tokens 截断 → 合法 JSON 的前缀（尾部落在一个字符串中间）。
_TRUNCATED_ARGS = _FULL_ARGS[: len(_FULL_ARGS) - 20]

_ENABLED = {mod.ENABLED_ENV: "1"}
_DISABLED = {mod.ENABLED_ENV: "0"}


def _broken_call(
    cid: str = "call_1",
    raw: str = _TRUNCATED_ARGS,
) -> ToolCallBlock:
    return ToolCallBlock(id=cid, name="Write", input=raw)


def _pair_msg(cid: str = "call_1", raw: str = _TRUNCATED_ARGS) -> Msg:
    """生产真实结构：同一轮推理的 tool_call + tool_result 在同一条 assistant 消息。

    用 ``model_construct`` 跳过框架级角色校验（与 tool_result_budget 测试一致）。
    """
    return Msg.model_construct(
        name="agent",
        role="assistant",
        content=[
            _broken_call(cid, raw),
            ToolResultBlock(
                id=cid,
                name="Write",
                output=[TextBlock(text="文件已写入")],
            ),
        ],
    )


class _FakeState:
    def __init__(self, context: list) -> None:
        self.session_id = "s1"
        self.reply_id = "r1"
        self.context = context


class _FakeAgent:
    def __init__(self, context: list) -> None:
        self.name = "agent"
        self.state = _FakeState(context)


async def _run(mw: ToolCallRepairMiddleware, agent: _FakeAgent, messages: list):
    """执行 on_model_call，返回 (next_handler 收到的 messages, 返回值)。"""
    received: dict = {}

    async def _next(**kwargs):
        received["messages"] = kwargs["messages"]
        return "model-response"

    result = await mw.on_model_call(agent, {"messages": messages}, _next)
    return received.get("messages"), result


# ---------------------------------------------------------------------------
# 开关：ADP_TOOL_CALL_REPAIR
# ---------------------------------------------------------------------------
class TestEnabledSwitch(IsolatedAsyncioTestCase):
    def test_default_enabled_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mod.ENABLED_ENV, None)
            self.assertTrue(tool_call_repair_enabled())

    def test_empty_value_falls_back_to_enabled(self):
        """部署模板里 ``VAR=`` 留空不应被误判为关闭。"""
        with patch.dict(os.environ, {mod.ENABLED_ENV: "  "}):
            self.assertTrue(tool_call_repair_enabled())

    def test_truthy_values(self):
        for raw in ("1", "true", "TRUE", "yes", "On"):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {mod.ENABLED_ENV: raw},
            ):
                self.assertTrue(tool_call_repair_enabled())

    def test_falsy_values(self):
        for raw in ("0", "false", "no", "off", "OFF"):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {mod.ENABLED_ENV: raw},
            ):
                self.assertFalse(tool_call_repair_enabled())


# ---------------------------------------------------------------------------
# 纯函数：repair_tool_call_input
# ---------------------------------------------------------------------------
class TestRepairToolCallInput(IsolatedAsyncioTestCase):
    def test_valid_input_returns_identical_string(self):
        """有效参数必须原样返回（字节一致，护住 prompt cache）。"""
        raw = '{"a": 1, "b": "中文", "c": [1, 2]}'
        fixed, verdict = repair_tool_call_input(raw)

        self.assertEqual(verdict, "ok")
        self.assertIs(fixed, raw)

    def test_empty_like_normalized_to_empty_object(self):
        for raw in ("", "   ", "null", "None"):
            with self.subTest(raw=raw):
                self.assertEqual(repair_tool_call_input(raw), ("{}", "empty"))

    def test_already_empty_object_is_untouched(self):
        """``{}`` 本身合法，不应被改写。"""
        self.assertEqual(repair_tool_call_input("{}"), ("{}", "ok"))

    def test_truncated_json_repaired(self):
        """线上形态：半截 JSON → 修复成非空 dict，关键字段保留。"""
        fixed, verdict = repair_tool_call_input(_TRUNCATED_ARGS)

        self.assertEqual(verdict, "repaired")
        self.assertIsNotNone(fixed)
        parsed = json.loads(fixed)  # 必须是合法 JSON
        self.assertEqual(
            parsed["file_path"],
            "/workspace/shared/skills/invoice-extractor/SKILL.md",
        )
        self.assertIn("invoice-extractor", parsed["content"])

    def test_repaired_output_is_deterministic(self):
        """同一坏串每轮必须产出同一字节，否则 prompt cache 会抖。"""
        first, _ = repair_tool_call_input(_TRUNCATED_ARGS)
        second, _ = repair_tool_call_input(_TRUNCATED_ARGS)

        self.assertEqual(first, second)

    def test_unrepairable_cases(self):
        for raw in ("{", "[1,2]", "[]", "not json at all"):
            with self.subTest(raw=raw):
                fixed, verdict = repair_tool_call_input(raw)
                self.assertIsNone(fixed)
                self.assertEqual(verdict, "unrepairable")

    def test_none_input_normalized(self):
        self.assertEqual(repair_tool_call_input(None), ("{}", "empty"))


# ---------------------------------------------------------------------------
# 中间件行为
# ---------------------------------------------------------------------------
class TestToolCallRepairMiddleware(IsolatedAsyncioTestCase):
    async def test_passthrough_when_disabled(self):
        agent = _FakeAgent([_pair_msg()])
        messages = list(agent.state.context)
        mw = ToolCallRepairMiddleware()

        with patch.dict(os.environ, _DISABLED):
            received, _ = await _run(mw, agent, messages)

        self.assertIs(received, messages)  # 纯透传
        # state 未被回写
        self.assertEqual(agent.state.context[0].content[0].input, _TRUNCATED_ARGS)

    async def test_all_valid_messages_not_rebuilt(self):
        """没有任何坏块时不能重建消息列表（避免无谓开销与缓存抖动）。"""
        agent = _FakeAgent([])
        msg = Msg(
            name="agent",
            role="assistant",
            content=[
                ToolCallBlock(id="c1", name="Read", input='{"file_path": "/a"}'),
            ],
        )
        messages = [msg]
        mw = ToolCallRepairMiddleware()

        with patch.dict(os.environ, _ENABLED):
            received, _ = await _run(mw, agent, messages)

        self.assertIs(received, messages)
        self.assertIs(received[0], msg)

    async def test_truncated_repaired_for_send_and_state(self):
        """发送副本与 state.context 都要被修好（一次修好永久生效）。"""
        msg = _pair_msg()
        agent = _FakeAgent([msg])
        messages = list(agent.state.context)
        mw = ToolCallRepairMiddleware()

        with patch.dict(os.environ, _ENABLED):
            received, _ = await _run(mw, agent, messages)

        # 1) 发送内容合法
        sent_call = received[0].content[0]
        self.assertEqual(sent_call.type, "tool_call")
        sent_args = json.loads(sent_call.input)
        self.assertIn("file_path", sent_args)
        # 2) 配对结构不变（tool_call + tool_result 都还在）
        self.assertEqual(len(received[0].content), 2)
        # 3) state 已回写：内容与发送副本一致，且不再等于原始坏串
        state_call = agent.state.context[0].content[0]
        self.assertNotEqual(state_call.input, _TRUNCATED_ARGS)
        self.assertEqual(json.loads(state_call.input), sent_args)

    async def test_empty_input_normalized_for_send_and_state(self):
        call = ToolCallBlock(id="c1", name="Bash", input="")
        msg = Msg(name="agent", role="assistant", content=[call])
        agent = _FakeAgent([msg])
        mw = ToolCallRepairMiddleware()

        with patch.dict(os.environ, _ENABLED):
            received, _ = await _run(mw, agent, [msg])

        self.assertEqual(received[0].content[0].input, "{}")
        self.assertEqual(agent.state.context[0].content[0].input, "{}")

    async def test_unrepairable_pair_dropped_from_send_copy_only(self):
        """不可修复 → 发送副本成对剔除；state 条数与内容都不动。"""
        msg = _pair_msg(raw="{")  # json_repair 只能修成空 dict → 视为不可修复
        agent = _FakeAgent([msg])
        mw = ToolCallRepairMiddleware()

        with patch.dict(os.environ, _ENABLED):
            received, _ = await _run(mw, agent, [msg])

        # 整条消息被剔空 → 不发送
        self.assertEqual(received, [])
        # state 未被改动（仍保留坏块与结果，便于排障）
        self.assertEqual(len(agent.state.context), 1)
        self.assertEqual(len(agent.state.context[0].content), 2)
        self.assertEqual(agent.state.context[0].content[0].input, "{")

    async def test_drop_disabled_keeps_broken_block(self):
        msg = Msg(
            name="agent",
            role="assistant",
            content=[ToolCallBlock(id="c1", name="Bash", input="{")],
        )
        agent = _FakeAgent([msg])
        mw = ToolCallRepairMiddleware()

        with (
            patch.dict(os.environ, _ENABLED),
            patch.object(mod, "_DROP_UNREPAIRABLE", False),
        ):
            received, _ = await _run(mw, agent, [msg])

        # 关掉剔除 = 保留现场：消息列表原样透传（连副本都不重建）
        self.assertIs(received[0], msg)
        self.assertEqual(len(received[0].content), 1)
        self.assertEqual(received[0].content[0].input, "{")

    async def test_write_back_disabled_only_fixes_send_copy(self):
        msg = _pair_msg()
        agent = _FakeAgent([msg])
        mw = ToolCallRepairMiddleware()

        with (
            patch.dict(os.environ, _ENABLED),
            patch.object(mod, "_WRITE_BACK_STATE", False),
        ):
            received, _ = await _run(mw, agent, [msg])

        self.assertIn("file_path", json.loads(received[0].content[0].input))
        # state 保持原样
        self.assertEqual(
            agent.state.context[0].content[0].input,
            _TRUNCATED_ARGS,
        )

    async def test_inspect_failure_does_not_block_call(self):
        """体检自身异常时只告警，请求照常发出。"""
        agent = _FakeAgent([])
        msg = Msg(name="agent", role="assistant", content=[_broken_call()])
        mw = ToolCallRepairMiddleware()

        with (
            patch.dict(os.environ, _ENABLED),
            patch(
                "bocomadp.middleware.tool_call_repair.repair_tool_call_input",
                side_effect=RuntimeError("boom"),
            ),
        ):
            received, result = await _run(mw, agent, [msg])

        self.assertEqual(result, "model-response")
        self.assertEqual(received, [msg])
