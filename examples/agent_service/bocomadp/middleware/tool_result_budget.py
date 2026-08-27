# -*- coding: utf-8 -*-
"""消息级聚合预算中间件(on_model_call, 复刻 Claude Code 聚合预算)。

发送给模型前,按 user 消息分组(以 assistant 消息为界)统计工具结果
字符合计;超过 ``message_budget_chars`` 时,把组内最大的、从未处理过的
结果持久化到 Redis 并替换为预览,循环直到预算内。

决策冻结:状态存 Redis Hash(``tool_result:replacement:{session_id}``):
- 已替换 → 重放缓存的替换文本(字节一致,prompt cache 前缀稳定);
- 已见未替换 → 冻结(永不替换);
- 新鲜 → 超预算时选最大的替换。
"""
from __future__ import annotations

import logging
from typing import Any

from agentscope.message import TextBlock
from agentscope.middleware import MiddlewareBase

from bocomadp.logging.agent_log_context import ctx_fields
from bocomadp.tool_result_message import build_persisted_message, extract_text_content
from bocomadp.tool_result_store import (
    PERSISTED_OUTPUT_TAG,
    get_replacement_state,
    get_tool_result_config,
    set_replacement_state,
    set_tool_result,
)

logger = logging.getLogger("as")


class _Candidate:
    """一个待评估的 tool_result 块。"""

    __slots__ = ("block", "text", "size")

    def __init__(self, block: Any, text: str) -> None:
        self.block = block
        self.text = text
        self.size = len(text)


def _iter_messages(messages: list[Any]):
    """按承载 tool_result 的消息分组,产出 (groups, tool_name_map)。

    对齐 CC 按 wire user 消息分组的语义:AgentScope 内部同一轮推理(含并行
    多工具)的 tool_call + tool_result 追加在同一条 assistant 消息里,聚合预算
    按这条消息合并评估(防 10×40K 一次灌入)。因此**任何承载 tool_result 的
    消息(assistant 或 user)独立成组**;user 承载是兼容测试路径(旧实现假设)。
    无 tool_result 的普通 user 输入并入当前组保持连续性,纯 assistant(推理/
    tool_call)消息闭合当前组但自身不入组。
    """
    groups: list[list[Any]] = []
    current: list[Any] = []
    name_by_id: dict[str, str] = {}
    for msg in messages:
        blocks = getattr(msg, "content", None)
        if not isinstance(blocks, list):
            continue
        has_tool_result = any(
            getattr(b, "type", None) == "tool_result" for b in blocks
        )
        for block in blocks:
            if getattr(block, "type", None) == "tool_call":
                name_by_id[block.id] = block.name
        if has_tool_result:
            # 承载 tool_result 的消息独立成组(先闭合当前组)
            if current:
                groups.append(current)
                current = []
            current.append(msg)
        elif getattr(msg, "role", None) == "user":
            # 普通 user 输入并入当前组(其前的 tool_result 组已闭合)
            current.append(msg)
        else:
            # 纯 assistant(推理/tool_call):闭合当前组,自身不入组
            if current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups, name_by_id


class ToolResultBudgetMiddleware(MiddlewareBase):  # pylint: disable=abstract-method
    """发送前按消息聚合预算替换超长工具结果。"""

    async def on_model_call(  # type: ignore[override]
        self,
        agent,
        input_kwargs: dict,
        next_handler,
    ):
        messages = input_kwargs["messages"]
        try:
            cfg = await get_tool_result_config()
        except Exception as exc:  # pragma: no cover - 配置读取异常兜底
            logger.warning("TOOL_RESULT_BUDGET cfg_failed %s err=%s", ctx_fields(agent), exc)
            cfg = None

        if cfg is None or not cfg.enabled:
            return await next_handler(**input_kwargs)

        session_id = agent.state.session_id
        ctx = ctx_fields(agent)
        try:
            state = await get_replacement_state(session_id)
        except Exception as exc:
            # Redis 不可用 → 不替换,原样发送(框架截断兜底)
            logger.warning("TOOL_RESULT_BUDGET state_read_failed %s err=%s", ctx, exc)
            return await next_handler(**input_kwargs)

        try:
            new_messages, state_changes = await self._enforce(
                messages,
                session_id,
                cfg,
                state,
            )
        except Exception as exc:
            logger.warning("TOOL_RESULT_BUDGET enforce_failed %s err=%s", ctx, exc)
            return await next_handler(**input_kwargs)

        if state_changes:
            # 被替换成预览的块(其 value 以 <persisted-output> 开头)与冻结块(空串)都
            # 记录,便于线上判断哪些 tool_result 被聚合预算处理。
            replaced_ids = [
                cid for cid, val in state_changes.items() if val.startswith(PERSISTED_OUTPUT_TAG)
            ]
            if replaced_ids:
                logger.info(
                    "TOOL_RESULT_BUDGET replaced %s tool_call_ids=%s",
                    ctx,
                    ",".join(replaced_ids),
                )
            try:
                await set_replacement_state(session_id, state_changes)
            except Exception as exc:
                logger.warning("TOOL_RESULT_BUDGET state_write_failed %s err=%s", ctx, exc)

        input_kwargs["messages"] = new_messages
        return await next_handler(**input_kwargs)

    async def _enforce(
        self,
        messages: list[Any],
        session_id: str,
        cfg: Any,
        state: dict[str, str],
    ) -> tuple[list[Any], dict[str, str]]:
        groups, name_by_id = _iter_messages(messages)
        replacement_map: dict[str, str] = {}
        state_changes: dict[str, str] = {}

        for group in groups:
            candidates: list[_Candidate] = []
            for msg in group:
                for block in getattr(msg, "content", []) or []:
                    if getattr(block, "type", None) != "tool_result":
                        continue
                    output = block.output
                    if isinstance(output, str):
                        text = output
                    else:
                        text = extract_text_content(output)
                        if text is None:  # 含 image/多模态 → 跳过
                            continue
                    if text.startswith(PERSISTED_OUTPUT_TAG):
                        continue  # per-tool 层已替换
                    tool_name = name_by_id.get(block.id) or block.name
                    if tool_name in cfg.exempt_tools:
                        continue
                    candidates.append(_Candidate(block, text))

            if not candidates:
                continue

            fresh: list[_Candidate] = []
            frozen: list[_Candidate] = []
            for c in candidates:
                cached = state.get(c.block.id)
                if cached == "":
                    frozen.append(c)  # 冻结:计入预算,不可替换
                    continue
                if cached:
                    replacement_map[c.block.id] = cached  # 重放:不计入预算
                    continue
                fresh.append(c)

            # 组内没有新结果(纯冻结/纯重放的历史组):对齐 CC(fresh 为空直接
            # 跳过,不计算预算、不做任何决策——冻结块不参与任何计算)
            if not fresh:
                continue

            # 组内合计 = 冻结块 + 新鲜块(重放块不计入,对齐 CC:frozenSize +
            # freshSize, mustReapply 完全排除;冻结块仅在"与 fresh 同组"时
            # 计入超预算判断,纯冻结历史组已被上方跳过)
            total = sum(c.size for c in frozen) + sum(c.size for c in fresh)
            if total <= cfg.message_budget_chars:
                # 全部标记已见(冻结),保证后续轮次决策稳定
                for c in candidates:
                    if c.block.id not in state and c.block.id not in replacement_map:
                        state_changes[c.block.id] = ""
                continue

            # 选最大的新鲜块替换,直到预算内
            fresh_sorted = sorted(fresh, key=lambda c: c.size, reverse=True)
            remaining = total
            for c in fresh_sorted:
                if remaining <= cfg.message_budget_chars:
                    break
                key = await set_tool_result(session_id, c.block.id, c.text)
                max_output_chars = min(
                    cfg.read_result_max_output_chars,
                    cfg.per_tool_threshold_chars,
                )
                preview = build_persisted_message(
                    key,
                    c.size,
                    c.text,
                    cfg.preview_chars,
                    tool_call_id=c.block.id,
                    max_output_chars=max_output_chars,
                )
                replacement_map[c.block.id] = preview
                state_changes[c.block.id] = preview
                remaining -= c.size

            for c in candidates:
                if c.block.id not in replacement_map and c.block.id not in state:
                    state_changes[c.block.id] = ""

        if not replacement_map:
            return messages, state_changes

        new_messages = self._apply_replacements(messages, replacement_map)
        return new_messages, state_changes

    @staticmethod
    def _apply_replacements(
        messages: list[Any],
        replacement_map: dict[str, str],
    ) -> list[Any]:
        """构建新消息列表并替换 tool_result 内容(不改原对象,避免污染
        state.context 与 UI 渲染;block 用 model_copy 产出新实例)。"""
        new_messages: list[Any] = []
        for msg in messages:
            blocks = getattr(msg, "content", None)
            if not isinstance(blocks, list):
                new_messages.append(msg)
                continue
            new_blocks: list[Any] = []
            replaced = False
            for block in blocks:
                if (
                    getattr(block, "type", None) == "tool_result"
                    and block.id in replacement_map
                ):
                    new_blocks.append(
                        block.model_copy(
                            update={
                                "output": [
                                    TextBlock(text=replacement_map[block.id]),
                                ],
                            },
                        ),
                    )
                    replaced = True
                else:
                    new_blocks.append(block)
            if replaced:
                new_messages.append(
                    msg.model_copy(update={"content": new_blocks}),
                )
            else:
                new_messages.append(msg)
        return new_messages
