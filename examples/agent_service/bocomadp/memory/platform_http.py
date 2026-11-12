# -*- coding: utf-8 -*-
"""记忆平台 HTTP 直连（普通 POST，JSON-RPC 载荷）。

与 :mod:`bocomadp.memory.platform`（SDK 门面）并列，但**不走 MCP 客户端 /
SDK**：按平台给出的 curl 示例直接 POST 一份 JSON-RPC 报文，自行解析响应。
平台响应可能是 ``application/json``，也可能是 ``text/event-stream``（SSE
``data: {...}`` 帧），两种都接。

错误统一收敛为 :class:`MemoryHttpError`（网络 / HTTP 状态码 / JSON-RPC
``isError`` / 不可解析响应），调用方（工具）据此返回可读错误而不是让
异常打断对话回合。
"""
from __future__ import annotations

import itertools
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("as")

#: JSON-RPC 请求 id：仅用于请求/响应配对，无业务含义（MCP 协议即 JSON-RPC）。
_request_ids = itertools.count(1)


class MemoryHttpError(RuntimeError):
    """记忆平台直连调用失败。"""


def build_jsonrpc_body(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """构造 ``tools/call`` 报文（``id`` 自增，便于日志排查）。"""
    return {
        "jsonrpc": "2.0",
        "id": next(_request_ids),
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def parse_response_text(text: str) -> dict[str, Any]:
    """解析响应体：JSON 直接解析；SSE 取最后一个非空 ``data:`` 帧。

    Raises:
        MemoryHttpError: 两种格式都解析不出 JSON 对象时。
    """
    stripped = (text or "").strip()
    if not stripped:
        raise MemoryHttpError("empty response body")
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise MemoryHttpError(f"invalid json response: {exc}") from exc
        if isinstance(data, dict):
            return data
        raise MemoryHttpError(f"unexpected json payload: {type(data).__name__}")

    last: str | None = None
    for line in stripped.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk and chunk != "[DONE]":
            last = chunk
    if last is None:
        raise MemoryHttpError(f"unrecognized response: {stripped[:200]!r}")
    try:
        data = json.loads(last)
    except json.JSONDecodeError as exc:
        raise MemoryHttpError(f"invalid sse data frame: {exc}") from exc
    if not isinstance(data, dict):
        raise MemoryHttpError(f"unexpected sse payload: {type(data).__name__}")
    return data


def extract_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """从 JSON-RPC 响应里取业务数据。

    优先 ``result.structuredContent``；缺失时解析 ``result.content[0].text``
    （平台会在 text 里再嵌一层 JSON 字符串）。
    ``result.isError`` 为真时抛 :class:`MemoryHttpError`。
    """
    if "error" in envelope and envelope.get("error"):
        raise MemoryHttpError(f"jsonrpc error: {envelope['error']}")

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise MemoryHttpError(f"missing result: {str(envelope)[:200]}")

    if result.get("isError"):
        texts = [
            str(block.get("text", ""))
            for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        raise MemoryHttpError(
            "platform tool error: " + ("".join(texts)[:300] or "unknown"),
        )

    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured

    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            data = json.loads(block.get("text") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


async def call_memory_tool(
    *,
    url: str,
    tool: str,
    arguments: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """POST 一次 ``tools/call``，返回业务 payload。

    Args:
        url: 接口地址（含 ``packageKey`` 查询串）。
        tool: ``tools/call`` 的工具名（检索 / 落库各一个）。
        arguments: 工具参数（平台要求包在 ``param`` 键内）。
        headers: 平台自定义请求头（agent-key / agentId / guwp-token 等）。
        timeout: 单次请求超时（秒）。

    Returns:
        `dict[str, Any]`: ``structuredContent``（或 text 内层 JSON）。

    Raises:
        MemoryHttpError: 网络、HTTP 状态码、JSON-RPC 或平台业务错误。
    """
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    request_headers.update(headers)
    body = build_jsonrpc_body(tool, arguments)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=body, headers=request_headers)
    except httpx.HTTPError as exc:
        raise MemoryHttpError(f"request failed: {exc}") from exc

    if response.status_code >= 400:
        raise MemoryHttpError(
            f"HTTP {response.status_code}: {response.text[:200]}",
        )
    envelope = parse_response_text(response.text)
    return extract_payload(envelope)


__all__ = [
    "MemoryHttpError",
    "build_jsonrpc_body",
    "parse_response_text",
    "extract_payload",
    "call_memory_tool",
]
