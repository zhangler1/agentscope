# -*- coding: utf-8 -*-
"""Task-package assembly and budget helpers for orchestrated workflows."""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

logger = logging.getLogger("bocomadp.team.protocol")

#: Hard cap for the short result summary submitted by a node.
RESULT_MAX_CHARS = 500

#: Total budget for one task message (task + inputs + system notes).
TASK_BUDGET_TOKENS = 10000


def estimate_tokens(text: str) -> int:
    """Conservative token estimate.

    CJK characters count as one token each; ASCII runs count as roughly
    four characters per token. This is only used for budget guardrails.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def truncate_text(text: str, max_tokens: int) -> tuple[str, bool]:
    """Truncate ``text`` to roughly ``max_tokens``; returns (text, truncated)."""
    if max_tokens <= 0:
        return "", bool(text)
    if estimate_tokens(text) <= max_tokens:
        return text, False
    # Binary-search the longest prefix that fits.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    suffix = "\n\n[... truncated by workflow budget ...]"
    return text[:low].rstrip() + suffix, True


def normalize_result(result: str) -> str:
    """Strip and cap a node result at :data:`RESULT_MAX_CHARS`."""
    text = (result or "").strip()
    if len(text) <= RESULT_MAX_CHARS:
        return text
    return text[: RESULT_MAX_CHARS - 3].rstrip() + "..."


def result_is_too_long(result: str) -> bool:
    return len((result or "").strip()) > RESULT_MAX_CHARS


def _format_inputs(inputs: Iterable[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for item in inputs or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "text")
        if kind == "file":
            path = str(item.get("path") or "")
            if path:
                lines.append(f"- 文件：{path}")
        else:
            content = str(item.get("content") or "")
            if content:
                lines.append(f"- {content}")
    return "\n".join(lines)


def build_task_prompt(
    task: str,
    inputs: Iterable[dict[str, Any]] | None = None,
    *,
    role: str = "",
    stage_note: str = "",
) -> str:
    """Build the message delivered to a downstream node.

    Only ``task`` + ``inputs`` are included. Role/global context belongs in
    the node's system prompt; this keeps the task message small.
    """
    parts: list[str] = []
    if role:
        parts.append(f"你的角色：{role}")
    if stage_note:
        parts.append(stage_note)
    parts.append("## 任务")
    parts.append(task.strip())
    input_text = _format_inputs(inputs)
    if input_text:
        parts.append("## 输入")
        parts.append(input_text)
    parts.append(
        "完成后调用 SubmitHandoffs 提交 result（≤500 字）"
        "和下游 handoffs（如果有下游）。",
    )
    text = "\n\n".join(parts).strip()
    truncated = False
    if estimate_tokens(text) > TASK_BUDGET_TOKENS:
        text, truncated = truncate_text(text, TASK_BUDGET_TOKENS)
    if truncated:
        logger.warning("task prompt truncated to %d tokens", TASK_BUDGET_TOKENS)
    return text


def build_results_prompt(results: list[dict[str, Any]]) -> str:
    """Build the leader-final / failed-summary input from short results."""
    lines = ["流程已结束。以下是各节点的简短汇报："]
    for item in results:
        target = item.get("from") or item.get("agent_id") or "?"
        stage = item.get("stage")
        stage_text = f"（阶段 {stage}）" if stage is not None else ""
        summary = str(item.get("summary") or "").strip()
        path = item.get("path")
        line = f"- {target}{stage_text}：{summary or '(无摘要)'}"
        if path:
            line += f"\n  完整内容：{path}"
        lines.append(line)
    lines.append(
        "\n请基于以上结果给用户最终答复；需要细节时可按路径读取共享工作区文件。"
    )
    return "\n".join(lines)


def build_failed_prompt(
    results: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> str:
    lines = [build_results_prompt(results), "\n## 失败节点"]
    for item in errors:
        lines.append(
            f"- {item.get('agent')}：{item.get('error') or '未知错误'}"
            f"（尝试 {item.get('attempts', '?')} 次）",
        )
    lines.append("\n请基于已有结果和失败原因给用户答复。")
    return "\n".join(lines)


def build_continue_prompt(task_summary: str, result_path: str | None = None) -> str:
    """Prompt used to resume an interrupted node (context-level, not exact)."""
    parts = [
        "你上次的任务被中断了。请从中断处继续，不要重复已完成步骤。",
        f"原任务：{task_summary or '(见会话历史)'}",
    ]
    if result_path:
        parts.append(f"已完成产物：{result_path}")
    parts.append("完成后调用 SubmitHandoffs 提交 result 和 handoffs。")
    return "\n".join(parts)


def dumps_inputs(inputs: Iterable[dict[str, Any]] | None) -> str:
    """Compact JSON representation for diagnostics."""
    return json.dumps(list(inputs or []), ensure_ascii=False)


__all__ = [
    "RESULT_MAX_CHARS",
    "TASK_BUDGET_TOKENS",
    "build_continue_prompt",
    "build_failed_prompt",
    "build_results_prompt",
    "build_task_prompt",
    "dumps_inputs",
    "estimate_tokens",
    "normalize_result",
    "result_is_too_long",
    "truncate_text",
]
