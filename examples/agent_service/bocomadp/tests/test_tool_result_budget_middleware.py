# -*- coding: utf-8 -*-
"""聚合预算中间件测试:按 user 消息分组、选最大替换、决策冻结重放。"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock

from bocomadp.config.app_config import ToolResultConfig
from bocomadp.middleware.tool_result_budget import ToolResultBudgetMiddleware


def _tool_result(rid: str, text: str) -> ToolResultBlock:
    return ToolResultBlock(id=rid, name="t", output=[TextBlock(text=text)])


def _tool_call(cid: str, name: str) -> ToolCallBlock:
    return ToolCallBlock(id=cid, name=name, input="")


def _assistant(calls: list[ToolCallBlock]) -> Msg:
    return Msg(name="agent", role="assistant", content=calls)


def _user_with_results(results: list[ToolResultBlock]) -> Msg:
    # Msg 的 user 角色校验只允许 text/data 块,而本中间件的消息模型中
    # tool_result 位于 user 消息;用 model_construct 跳过框架级校验。
    return Msg.model_construct(name="user", role="user", content=results)


def _assistant_with_call_and_results(
    calls: list[ToolCallBlock],
    results: list[ToolResultBlock],
) -> Msg:
    """生产真实结构:同一轮推理的 tool_call + tool_result 追加在同一条
    assistant 消息里(AgentScope append_context 行为)。用 model_construct
    跳过框架级角色校验。"""
    return Msg.model_construct(
        name="agent",
        role="assistant",
        content=calls + results,
    )


class _FakeAgent:
    def __init__(self, session_id: str = "s1") -> None:
        self.state = type("State", (), {"session_id": session_id})()


async def _run(mw, agent, messages):
    """执行 on_model_call,返回 next_handler 收到的 messages 与返回值。"""
    received = {}

    async def _next(**kwargs):
        received["messages"] = kwargs["messages"]
        return "model-response"

    result = await mw.on_model_call(agent, {"messages": messages}, _next)
    return received.get("messages"), result


class TestToolResultBudgetMiddleware(IsolatedAsyncioTestCase):
    async def test_disabled_passthrough(self):
        mw = ToolResultBudgetMiddleware()
        messages = [_user_with_results([_tool_result("r1", "x" * 100000)])]
        with patch(
            "bocomadp.middleware.tool_result_budget.get_tool_result_config",
            new=AsyncMock(return_value=ToolResultConfig(enabled=False)),
        ):
            sent, result = await _run(mw, _FakeAgent(), messages)

        assert result == "model-response"
        assert sent is messages  # 同一实例,未修改

    async def test_over_budget_replaces_largest_fresh(self):
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant([_tool_call("c1", "a"), _tool_call("c2", "b")]),
            _user_with_results(
                [_tool_result("r1", "x" * 70000), _tool_result("r2", "y" * 70000)],
            ),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig(message_budget_chars=100000)),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(side_effect=lambda sid, cid, content: f"tool_result:{sid}:{cid}"),
            ) as setter,
            patch(
                "bocomadp.middleware.tool_result_budget.set_replacement_state",
                new=AsyncMock(),
            ) as set_state,
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        # 140K > 100K → 替换最大的 1 个(70K),剩余 70K 在预算内
        setter.assert_awaited_once()
        user_msg = sent[1]
        replaced = [b for b in user_msg.content if b.id == "r1"][0]
        other = [b for b in user_msg.content if b.id == "r2"][0]
        assert replaced.output[0].text.startswith("<persisted-output>")
        assert other.output[0].text == "y" * 70000
        # 决策状态:被替换的写文本,未替换的写冻结标记
        set_state.assert_awaited_once()
        mapping = set_state.await_args.args[1]
        assert mapping["r1"].startswith("<persisted-output>")
        assert mapping["r2"] == ""

    async def test_reapply_cached_replacement_byte_identical(self):
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant([_tool_call("c1", "a")]),
            _user_with_results([_tool_result("r1", "x" * 70000)]),
        ]
        cached = (
            "<persisted-output>\n输出过大(70.0KB),完整内容已保存至: k\n...\n\n"
            "如需完整内容,请调用 read_tool_result 工具读取"
            "(tool_call_id = \"r1\",支持 offset/limit 分页)。\n</persisted-output>"
        )
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig()),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={"r1": cached}),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(),
            ) as setter,
            patch(
                "bocomadp.middleware.tool_result_budget.set_replacement_state",
                new=AsyncMock(),
            ) as set_state,
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        setter.assert_not_awaited()  # 不再写 Redis
        set_state.assert_not_awaited()  # 状态无变化
        block = sent[1].content[0]
        assert block.output[0].text == cached  # 字节一致重放

    async def test_replayed_large_result_not_counted_in_budget(self):
        """重放块(消息里仍是原文大尺寸)不计入预算:对齐 CC(mustReapply 排除)。

        回归:旧实现 total 计入重放块,导致 fresh 30K + 重放原文 70K = 100K
        > 预算 50K 时错误替换 fresh;CC 语义 total 只计 frozen + fresh。
        """
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant([_tool_call("c1", "a"), _tool_call("c2", "b")]),
            _user_with_results(
                [
                    _tool_result("r1", "x" * 70000),  # 已替换过(状态有缓存),消息里仍是原文
                    _tool_result("r2", "y" * 30000),  # 本轮新增
                ],
            ),
        ]
        cached = (
            "<persisted-output>\n输出过大(70.0KB),完整内容已保存至: k\n...\n\n"
            "如需完整内容,请调用 read_tool_result 工具读取"
            "(tool_call_id = \"r1\",支持 offset/limit 分页)。\n</persisted-output>"
        )
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(
                    return_value=ToolResultConfig(message_budget_chars=50000),
                ),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={"r1": cached}),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(),
            ) as setter,
            patch(
                "bocomadp.middleware.tool_result_budget.set_replacement_state",
                new=AsyncMock(),
            ) as set_state,
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        # 仅 fresh 30K ≤ 50K 预算 → 不替换;重放块字节一致重放;
        # fresh 的 r2 被标记冻结(seen),保证后续轮次决策稳定(与 CC 一致)
        setter.assert_not_awaited()
        set_state.assert_awaited_once()
        mapping = set_state.await_args.args[1]
        assert mapping == {"r2": ""}
        blocks = {b.id: b for b in sent[1].content}
        assert blocks["r1"].output[0].text == cached  # 重放
        assert blocks["r2"].output[0].text == "y" * 30000  # fresh 未被误替换

    async def test_frozen_large_result_counts_in_budget(self):
        """冻结块计入预算判断(CC frozenSize):冻结 40K + fresh 30K > 预算 50K
        → 触发替换 fresh;若冻结不计入则 total=30K ≤ 50K 不会替换。"""
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant([_tool_call("c1", "a"), _tool_call("c2", "b")]),
            _user_with_results(
                [
                    _tool_result("r1", "x" * 40000),  # 已见未替换(冻结,消息里仍是原文)
                    _tool_result("r2", "y" * 30000),  # 本轮新增(fresh)
                ],
            ),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(
                    return_value=ToolResultConfig(message_budget_chars=50000),
                ),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={"r1": ""}),  # 冻结标记
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(return_value="tool_result:s1:r2"),
            ) as setter,
            patch(
                "bocomadp.middleware.tool_result_budget.set_replacement_state",
                new=AsyncMock(),
            ),
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        # total = 40K(frozen) + 30K(fresh) = 70K > 50K → 替换唯一的 fresh r2
        setter.assert_awaited_once_with("s1", "r2", "y" * 30000)
        blocks = {b.id: b for b in sent[1].content}
        assert blocks["r1"].output[0].text == "x" * 40000  # 冻结块原文不动
        assert blocks["r2"].output[0].text.startswith("<persisted-output>")

    async def test_already_persisted_preview_not_double_processed(self):
        mw = ToolResultBudgetMiddleware()
        preview = (
            "<persisted-output>\n输出过大(1.0MB),完整内容已保存至: k\n"
            "预览(前 2000 字符):\nabc\n...\n\n"
            "如需完整内容,请调用 read_tool_result 工具读取"
            "(tool_call_id = \"r1\",支持 offset/limit 分页)。\n</persisted-output>"
        )
        messages = [
            _assistant([_tool_call("c1", "a")]),
            _user_with_results([_tool_result("r1", preview)]),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig(message_budget_chars=10)),  # 任何合计都超
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(),
            ) as setter,
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        setter.assert_not_awaited()  # 已持久化块跳过
        assert sent[1].content[0].output[0].text == preview

    async def test_redis_failure_no_replace(self):
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant([_tool_call("c1", "a")]),
            _user_with_results([_tool_result("r1", "x" * 70000)]),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig(message_budget_chars=100000)),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(side_effect=ConnectionError("redis down")),
            ),
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        assert sent[1].content[0].output[0].text == "x" * 70000  # 原样

    async def test_assistant_carried_tool_result_replaced(self):
        """生产路径:tool_result 承载在 assistant 消息(同一轮 tool_call +
        tool_result 合并) → 聚合预算需按整条消息评估并替换最大块。
        旧 _iter_messages 只收集 user 消息,此场景候选为空导致不压缩。"""
        mw = ToolResultBudgetMiddleware()
        messages = [
            _assistant_with_call_and_results(
                [_tool_call("c1", "a"), _tool_call("c2", "b")],
                [_tool_result("r1", "x" * 70000), _tool_result("r2", "y" * 70000)],
            ),
        ]
        with (
            patch(
                "bocomadp.middleware.tool_result_budget.get_tool_result_config",
                new=AsyncMock(return_value=ToolResultConfig(message_budget_chars=100000)),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.get_replacement_state",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "bocomadp.middleware.tool_result_budget.set_tool_result",
                new=AsyncMock(side_effect=lambda sid, cid, content: f"tool_result:{sid}:{cid}"),
            ) as setter,
            patch(
                "bocomadp.middleware.tool_result_budget.set_replacement_state",
                new=AsyncMock(),
            ) as set_state,
        ):
            sent, _ = await _run(mw, _FakeAgent(), messages)

        # 140K > 100K → 替换最大的 1 个(70K),剩余 70K 在预算内
        setter.assert_awaited_once()
        asst_msg = sent[0]
        replaced = [b for b in asst_msg.content if b.id == "r1"][0]
        other = [b for b in asst_msg.content if b.id == "r2"][0]
        assert replaced.output[0].text.startswith("<persisted-output>")
        assert other.output[0].text == "y" * 70000
        set_state.assert_awaited_once()
        mapping = set_state.await_args.args[1]
        assert mapping["r1"].startswith("<persisted-output>")
        assert mapping["r2"] == ""
