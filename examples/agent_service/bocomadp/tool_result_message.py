# -*- coding: utf-8 -*-
"""工具结果文本提取与 <persisted-output> 预览消息构建。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .tool_result_store import (
    PERSISTED_OUTPUT_CLOSING_TAG,
    PERSISTED_OUTPUT_TAG,
)


def extract_text_content(content: str | Sequence[Any]) -> str | None:
    """把 ToolResponse.content / ToolResultBlock.output 提取为纯文本。

    输入为 str 或 ``list[TextBlock | DataBlock]``;含 DataBlock(图片/多模态)
    返回 None —— 调用方应原样透传,不持久化。
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            return None
    return "".join(parts)


def generate_preview(content: str, max_chars: int) -> tuple[str, bool]:
    """取前 ``max_chars`` 字符;若截断,优先在边界内最后一个换行处断行
    (换行位置需位于后半段,否则直接硬截断)——避免切在行中间。
    """
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_newline = truncated.rfind("\n")
    cut = last_newline if last_newline > max_chars * 0.5 else max_chars
    return content[:cut], True


def format_size(chars: int) -> str:
    """字符数的人类可读大小(近似字节)。"""
    if chars >= 1024 * 1024:
        return f"{chars / 1024 / 1024:.1f}MB"
    if chars >= 1024:
        return f"{chars / 1024:.1f}KB"
    return f"{chars}B"


def build_persisted_message(
    key: str,
    original_size: int,
    content: str,
    preview_chars: int = 2000,
    tool_call_id: str = "",
    max_output_chars: int = 100_000,
) -> str:
    """构建给模型看的 <persisted-output> 预览消息(复刻 Claude Code 格式)。

    Args:
        key: Redis 中完整内容的键(``tool_result:{session_id}:{tool_call_id}``)。
        original_size: 原始内容字符数。
        content: 完整内容(仅用于生成预览)。
        preview_chars: 预览保留的字符数。
        tool_call_id: 本次工具调用 id,读回提示中显式给出,模型据此调用
            ``read_tool_result``。
        max_output_chars: 单次读回输出上限(字符),由系统配置
            ``read_result_max_output_chars``(实际取 min 与
            ``per_tool_threshold_chars``)决定;提示词据此告诉模型
            ``read_tool_result`` 单次最多可读多少内容。
    """
    preview, has_more = generate_preview(content, preview_chars)
    read_back = (
        f"如需完整内容,请调用 read_tool_result 工具读取,"
        f"务必使用 offset/limit 分页参数分批取回,严禁一次读取全部内容"
        f"(tool_call_id = \"{tool_call_id}\";"
        f"read_tool_result 单次最多可读取 {max_output_chars} 字符,"
        f"建议每页 limit 设为 {max_output_chars} 以内,"
        f"先读 offset=0 的首页,再按需以 offset 递增继续)。\n"
        f"{PERSISTED_OUTPUT_CLOSING_TAG}"
    )
    msg = (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"输出过大({format_size(original_size)}),完整内容已保存至: {key}\n\n"
        f"预览(前 {preview_chars} 字符):\n"
        f"{preview}"
    )
    if has_more:
        msg += "\n..."
    msg += "\n\n" + read_back
    return msg


__all__ = ["extract_text_content", "generate_preview", "format_size", "build_persisted_message"]
