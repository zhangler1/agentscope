# -*- coding: utf-8 -*-
"""ToolCallRepairMiddleware —— 发送前对历史 tool_call 参数做「体检 + 自愈」。

背景（线上事故）：模型输出被 ``max_tokens`` 截断、或流式被中断时，
``ToolCallBlock.input`` 会是一段**半截 JSON**。框架只在「执行工具」那一次用
``json_repair`` 兜底（``agentscope._utils._common._json_loads_with_repair``），
且**不回写** block，于是坏字符串永久留在 ``agent.state.context`` 里；此后每次
模型调用都被原样重发 → 上游网关解析 ``tool_calls[].function.arguments`` 失败
（Python ``json.JSONDecodeError`` → HTTP 400）→ 本地归类为
``invalid_request``，且**同一会话每轮都报同一个错**，会话彻底卡死。

本中间件挂在 ``on_model_call`` 最外层（先于其它中间件、也先于 event_log /
Langfuse 的记录点），在请求发出前遍历将被发送的消息，对每个
``ToolCallBlock`` 做四选一处置：

1. ``input`` 为空串 / ``"null"`` / ``"None"`` → 规范化为 ``"{}"``
   （无参工具调用的正常形态；空串本身也会被上游 ``json.loads`` 拒收）；
2. ``json.loads`` 成功且是 dict → **原样不动**（字节一致，护住 prompt cache）；
3. 解析失败但 ``json_repair`` 能产出**非空 dict** → 用确定性序列化的结果替换，
   并**回写 ``agent.state.context``**（run 结束时随 ``update_session_state``
   落库，一次修好永久生效）。执行侧本来用的就是修复后的参数，回写使历史自洽；
4. 完全无法修复（repair 抛错 / 结果不是 dict / 是空 dict）→ 成对剔除该
   ``tool_call`` 与其 ``tool_result``，**仅作用于本次发送副本**，不改
   ``state.context`` 的条数。必须成对：只删 tool_call 会留下孤儿
   ``role=tool`` 消息，同样被上游拒收（且判定是确定性的，每轮剔除结果稳定）。

所有处置都打一条带 session/reply/agent/user/run + tool_call_id/tool_name 的
日志；体检自身异常只告警、绝不阻断主流程。

开关（``ADP_TOOL_CALL_REPAIR``，与 ``ADP_K8S_SLOT_RELEASE_ON_RUN_END`` 同风格）：

- 未设置 / 空值 / ``1`` / ``true`` / ``yes`` / ``on`` → 开启（默认）；
- ``0`` / ``false`` / ``no`` / ``off`` → 关闭（纯透传，快速回退）。

另两个决策点是代码常量（``_WRITE_BACK_STATE`` / ``_DROP_UNREPAIRABLE``），
需要变更时改代码即可 —— 它们不是线上止血用的开关。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from agentscope.middleware import MiddlewareBase

from ..logging.agent_log_context import ctx_fields

logger = logging.getLogger("as")

#: 开关环境变量名（默认开启；写 0/false/off 关闭）。
ENABLED_ENV = "ADP_TOOL_CALL_REPAIR"

#: 视为「无参调用」的 input 形态（规范化为 ``{}``）。
#: 注意不含 ``"{}"``——它本身是合法 JSON，走 ``"ok"`` 分支保持字节一致，
#: 避免每轮无谓重建消息列表。
_EMPTY_LIKE = frozenset({"", "null", "None"})

#: 视为「开启」的取值（大小写不敏感）。
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: 修复结果是否回写 ``agent.state.context``（一次修好永久生效）。
#: ``False`` 时只修本次发送副本，不动持久化状态。
_WRITE_BACK_STATE = True

#: 完全无法修复时是否成对剔除（仅作用于本次发送副本）。
#: ``False`` 时保留现场（请求大概率仍被上游拒收，用于排障对比）。
_DROP_UNREPAIRABLE = True

#: 日志里附带的原始串头部/尾部长度。
_RAW_HEAD = 200
_RAW_TAIL = 120


def tool_call_repair_enabled() -> bool:
    """读取开关：未设置 / 空值视为开启（与部署模板里 ``VAR=`` 留空兼容）。"""
    raw = os.getenv(ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw in _TRUTHY


def repair_tool_call_input(raw: str | None) -> tuple[str | None, str]:
    """体检单个 ``tool_call.input``。

    纯函数，无副作用，便于单测与被其它组件复用。

    Args:
        raw (`str | None`): ``ToolCallBlock.input`` 的原始字符串。

    Returns:
        `tuple[str | None, str]`: ``(修复后的串 | None, verdict)``，其中
        verdict 取值：

        - ``"ok"``：本来就是合法 dict → 返回原始串（**字节一致**）；
        - ``"empty"``：空/``null`` 形态 → 返回 ``"{}"``；
        - ``"repaired"``：半截 JSON 被 ``json_repair`` 修成非空 dict；
        - ``"unrepairable"``：返回 ``None``，调用方应成对剔除。
    """
    raw = raw or ""
    if raw.strip() in _EMPTY_LIKE:
        return "{}", "empty"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        # 有效：原样返回，避免任何字节改动（prompt cache 前缀必须稳定）。
        return raw, "ok"

    try:
        # 与执行侧同源修复（见 agentscope._utils._common._json_loads_with_repair）。
        from json_repair import repair_json

        parsed = json.loads(repair_json(raw, stream_stable=True))
    except Exception:  # noqa: BLE001 —— 任何修复失败都视为不可用
        return None, "unrepairable"
    if not isinstance(parsed, dict) or not parsed:
        # 非 dict（如 "[1,2]"）或修成空 dict（如 "{"）：无从判断原始参数，
        # 不伪造 "{}"，交给调用方剔除。
        return None, "unrepairable"
    # 确定性序列化：同一坏串每轮产出同一字节，不会造成 prompt cache 抖动。
    return json.dumps(parsed, ensure_ascii=False), "repaired"


def drop_tool_call_pairs(
    messages: list[Any],
    ids: set[str],
) -> list[Any]:
    """按 id 成对剔除 ``tool_call`` 与其 ``tool_result``（返回副本）。

    ``ToolResultBlock.id`` 即对应 ``ToolCallBlock.id``（见
    ``agentscope.agent._agent._execute_tool_call``），因此一个 id 集合可同时
    命中两侧。整条消息被剔空时直接不发送（DeepSeek formatter 对空
    assistant 消息本就会跳过，这里提前收敛更直观）。

    Args:
        messages (`list[Any]`): 待过滤的消息列表。
        ids (`set[str]`): 需要成对剔除的 tool_call id 集合。

    Returns:
        `list[Any]`: 过滤后的新列表；无命中时返回原对象。
    """
    if not ids:
        return messages
    out: list[Any] = []
    changed = False
    for msg in messages:
        blocks = getattr(msg, "content", None)
        if not isinstance(blocks, list):
            out.append(msg)
            continue

        kept = [
            block
            for block in blocks
            if not (
                getattr(block, "type", None) in ("tool_call", "tool_result")
                and getattr(block, "id", None) in ids
            )
        ]
        if len(kept) == len(blocks):
            out.append(msg)
            continue
        changed = True
        if not kept:
            continue
        out.append(msg.model_copy(update={"content": kept}))
    return out if changed else messages


def sanitize_tool_calls(
    messages: list[Any],
    *,
    drop_unrepairable: bool = True,
) -> tuple[list[Any], dict[str, str], set[str], list[tuple[str, str, str, str]]]:
    """对消息列表做 tool_call 参数体检（不依赖 agent，任何调用点可复用）。

    与 :class:`ToolCallRepairMiddleware` 同一套判定，供拿不到 ``agent`` 的
    发送路径使用（例如 provider 的 ``_call_api``：上下文压缩直接调
    ``generate_structured_output``，不经过 ``on_model_call`` 中间件链）。

    Args:
        messages (`list[Any]`):
            待发送的消息列表（框架 ``Msg`` 对象）。
        drop_unrepairable (`bool`, defaults to ``True``):
            无法修复时是否成对剔除（仅作用于返回副本）。

    Returns:
        `tuple[list[Any], dict[str, str], set[str], list[tuple[str, str, str, str]]]`:
        ``(发送用消息列表, tool_call_id → 修复后的串, 被剔除的 id 集合,
        处置明细)``；处置明细元素为
        ``(verdict, tool_call_id, tool_name, raw)``，``verdict`` 取值
        ``"repaired"`` / ``"empty"`` / ``"unrepairable_kept"`` /
        ``"unrepairable_dropped"``。无任何改动时原样返回入参。
    """
    fixes: dict[str, str] = {}
    drop_ids: set[str] = set()
    actions: list[tuple[str, str, str, str]] = []
    out: list[Any] = []
    changed = False

    for msg in messages:
        blocks = getattr(msg, "content", None)
        if not isinstance(blocks, list):
            out.append(msg)
            continue

        new_blocks: list[Any] = []
        replaced_any = False
        for block in blocks:
            if getattr(block, "type", None) != "tool_call":
                new_blocks.append(block)
                continue

            raw = block.input or ""
            fixed, verdict = repair_tool_call_input(raw)
            if verdict == "ok":
                new_blocks.append(block)
                continue

            if verdict == "unrepairable":
                if not drop_unrepairable:
                    new_blocks.append(block)
                    actions.append(
                        ("unrepairable_kept", block.id, block.name, raw),
                    )
                    continue
                drop_ids.add(block.id)
                replaced_any = True
                actions.append(
                    ("unrepairable_dropped", block.id, block.name, raw),
                )
                continue

            # "empty" / "repaired"：这两个分支 repair 必返回字符串
            assert fixed is not None
            new_blocks.append(block.model_copy(update={"input": fixed}))
            replaced_any = True
            fixes[block.id] = fixed
            actions.append((verdict, block.id, block.name, raw))

        if replaced_any:
            changed = True
            out.append(msg.model_copy(update={"content": new_blocks}))
        else:
            out.append(msg)

    if drop_ids:
        out = drop_tool_call_pairs(out, drop_ids)
        changed = True
    return (out if changed else messages), fixes, drop_ids, actions


class ToolCallRepairMiddleware(MiddlewareBase):  # pylint: disable=abstract-method
    """见模块 docstring：发送前修复/剔除不可用的工具调用参数。"""

    async def on_model_call(  # type: ignore[override]
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ):
        """体检待发送消息 → 透传（必要时替换 messages 副本）。"""
        if not tool_call_repair_enabled():
            return await next_handler(**input_kwargs)

        messages = input_kwargs.get("messages") or []

        try:
            new_messages, fixes, drop_ids = self._inspect(
                agent,
                messages,
                drop_unrepairable=_DROP_UNREPAIRABLE,
            )
        except Exception as exc:  # noqa: BLE001 —— 体检失败绝不阻断主流程
            logger.warning(
                "TOOL_CALL_REPAIR inspect_failed %s err=%s",
                ctx_fields(agent),
                exc,
            )
            return await next_handler(**input_kwargs)

        if drop_ids:
            new_messages = self._drop_pairs(new_messages, drop_ids)

        if fixes and _WRITE_BACK_STATE:
            self._write_back(agent, fixes)

        if new_messages is not messages:
            input_kwargs["messages"] = new_messages

        return await next_handler(**input_kwargs)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _inspect(
        self,
        agent: Any,
        messages: list[Any],
        drop_unrepairable: bool = True,
    ) -> tuple[list[Any], dict[str, str], set[str]]:
        """扫描一遍消息列表。

        Args:
            agent (`Any`): 当前 agent（仅用于日志上下文）。
            messages (`list[Any]`): 待发送的消息列表。
            drop_unrepairable (`bool`, defaults to `True`): 不可修复时是否从
                发送副本剔除该块；``False`` 时原样保留（请求可能仍被上游拒收，
                但保留现场便于排障）。

        Returns:
            `tuple[list[Any], dict[str, str], set[str]]`:
            ``(发送用消息列表, tool_call_id → 修复后的串, 待剔除的 id 集合)``。
            没有任何改动时**原样返回入参对象**，调用方可据此跳过替换。
        """
        sanitized, fixes, drop_ids, actions = sanitize_tool_calls(
            messages,
            drop_unrepairable=drop_unrepairable,
        )
        if not actions:
            return sanitized, fixes, drop_ids

        ctx = ctx_fields(agent)
        for verdict, tool_call_id, tool_name, raw in actions:
            if verdict == "unrepairable_kept":
                # 保留现场：请求大概率仍被上游拒收，但由运维显式选择。
                logger.warning(
                    "TOOL_CALL_REPAIR keep_unrepairable %s tool=%s "
                    "tool_call_id=%s raw_len=%d raw_head=%r",
                    ctx,
                    tool_name,
                    tool_call_id,
                    len(raw),
                    raw[:_RAW_HEAD],
                )
            elif verdict == "unrepairable_dropped":
                logger.warning(
                    "TOOL_CALL_REPAIR drop %s tool=%s tool_call_id=%s "
                    "raw_len=%d raw_head=%r",
                    ctx,
                    tool_name,
                    tool_call_id,
                    len(raw),
                    raw[:_RAW_HEAD],
                )
            elif verdict == "repaired":
                logger.warning(
                    "TOOL_CALL_REPAIR repaired %s tool=%s "
                    "tool_call_id=%s raw_len=%d closed=%s raw_tail=%r",
                    ctx,
                    tool_name,
                    tool_call_id,
                    len(raw),
                    raw.rstrip().endswith("}"),
                    raw[-_RAW_TAIL:],
                )
            else:  # "empty"
                logger.info(
                    "TOOL_CALL_REPAIR normalized %s tool=%s "
                    "tool_call_id=%s raw=%r",
                    ctx,
                    tool_name,
                    tool_call_id,
                    raw,
                )
        return sanitized, fixes, drop_ids

    @staticmethod
    def _drop_pairs(messages: list[Any], ids: set[str]) -> list[Any]:
        """按 id 成对剔除 tool_call 与其 tool_result（见模块函数实现）。"""
        return drop_tool_call_pairs(messages, ids)

    @staticmethod
    def _write_back(agent: Any, fixes: dict[str, str]) -> None:
        """把修复结果原地写回 ``agent.state.context``。

        不能依赖 ``input_kwargs["messages"]`` 与 ``state.context`` 共享对象引用
        （上游中间件可能已用 ``model_copy`` 造了新列表），因此显式按 id 回写。
        run 结束时 ``ChatService`` 会把 ``agent.state`` 整体落库
        （含失败路径），所以修复只需发生一次。
        """
        state = getattr(agent, "state", None)
        if state is None:
            return
        for msg in getattr(state, "context", None) or []:
            for block in getattr(msg, "content", None) or []:
                if (
                    getattr(block, "type", None) == "tool_call"
                    and getattr(block, "id", None) in fixes
                ):
                    block.input = fixes[block.id]


__all__ = [
    "ENABLED_ENV",
    "ToolCallRepairMiddleware",
    "drop_tool_call_pairs",
    "repair_tool_call_input",
    "sanitize_tool_calls",
    "tool_call_repair_enabled",
]
