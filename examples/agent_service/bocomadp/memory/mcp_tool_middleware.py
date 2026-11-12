# -*- coding: utf-8 -*-
"""记忆 MCP 工具中间件：业务参数规范化与对话窗口组装。

挂在 ``MCPTool`` 实例上（``ToolBase.__call__`` 的洋葱链），在真正的
``ClientSession.call_tool`` 之前改写 ``arguments``：

- ``EUVDsearchMemory``：``{keyword, topK}`` → ``{"param": {keyword, topK}}``，
  补齐默认值并夹到配置上限（平台 schema 的 maximum 为 20）；
- ``EUVDsaveMemoriesStandard``：``{rounds}`` → 查 message 表组装最近
  ``rounds`` 轮对话（含本轮 user），连同固定 ``memoryType=1`` /
  ``inferFlag=1`` 一起包成 ``{"param": ...}``。

身份字段（agentId / agentPlat / runId / guwp-token）全部由请求头注入，
不在这里处理。

注意：工具调用发生在模型生成过程中，本轮 assistant 尚未落库——窗口
天然只覆盖"本轮 user 之前"的完整轮，与既有记忆提取语义一致。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk, ToolMiddlewareBase

from bocomadp.config import get_app_config
from bocomadp.memory.config import MemoryGlobalConfig
from bocomadp.memory.extractor import (
    _load_recent_messages,
    _truncate_to_budget,
)
from bocomadp.memory.global_config_store import get_memory_global_config

logger = logging.getLogger("as")

#: 平台 schema 对 topK 的硬上限（max 20），配置值不能超过它。
_PLATFORM_MAX_TOP_K = 20

#: 提取窗口最多回溯的消息条数。
_MAX_WINDOW_MESSAGES = 800

#: 平台固定业务参数。
_MEMORY_TYPE = 1
_INFER_FLAG = 1

#: 框架 storage（main.py 启动时注入）。
_storage: Any = None


def set_memory_tool_storage(storage: Any) -> None:
    """注入框架 storage（main.py 启动时调用一次）。"""
    global _storage
    _storage = storage


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------
def _ctx_ids() -> tuple[str, str]:
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_user_id,
    )

    return (
        (_current_user_id.get() or "").strip(),
        (_current_session_id.get() or "").strip(),
    )


def _error(text: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=text)],
        state=ToolResultState.ERROR,
    )


# ---------------------------------------------------------------------------
# 消息窗口
# ---------------------------------------------------------------------------
def _text_of(msg: Any) -> str:
    try:
        return (msg.get_text_content() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_extract_payload(
    messages: list[Any],
    rounds: int,
) -> tuple[list[dict[str, str]], str]:
    """构造提取请求的 ``messages`` 数组与 ``runId``。

    规则与既有实现一致：只保留非空 user/assistant；以最后一条 assistant
    为锚向前按 assistant 切块（块内允许连续多条 user），取最后 ``rounds``
    块；末尾追加本轮 user。``runId`` 为最后一块末条 assistant 的 id，无
    块时回退本轮 user 的 id。
    """
    valid: list[tuple[Any, str, str]] = []
    for msg in messages:
        role = str(getattr(msg, "role", "") or "")
        if role not in ("user", "assistant"):
            continue
        text = _text_of(msg)
        if not text:
            continue
        valid.append((msg, role, text))

    last_asst = -1
    for index, (_msg, role, _text) in enumerate(valid):
        if role == "assistant":
            last_asst = index

    if last_asst >= 0:
        tail_users = [
            item for item in valid[last_asst + 1 :] if item[1] == "user"
        ]
    else:
        tail_users = [item for item in valid if item[1] == "user"]

    blocks: list[list[tuple[Any, str, str]]] = []
    cursor = last_asst
    while cursor >= 0 and len(blocks) < rounds:
        if valid[cursor][1] != "assistant":
            cursor -= 1
            continue
        end = cursor
        start = cursor - 1
        while start >= 0 and valid[start][1] != "assistant":
            start -= 1
        blocks.append(valid[start + 1 : end + 1])
        cursor = start
    blocks.reverse()

    payload = [
        {"role": role, "content": text}
        for block in blocks
        for _msg, role, text in block
    ]
    payload.extend(
        {"role": role, "content": text} for _msg, role, text in tail_users
    )

    if blocks and blocks[-1]:
        last_msg = blocks[-1][-1][0]
    elif tail_users:
        last_msg = tail_users[-1][0]
    else:
        return payload, ""
    return payload, str(getattr(last_msg, "id", "") or "")


async def _load_window(
    user_id: str,
    session_id: str,
    rounds: int,
) -> list[Any]:
    if _storage is None:
        return []
    want = min(_MAX_WINDOW_MESSAGES, max(50, rounds * 6))
    return await _load_recent_messages(
        _storage,
        user_id,
        session_id,
        want,
    )


# ---------------------------------------------------------------------------
# 参数规范化
# ---------------------------------------------------------------------------
def normalize_search_args(
    business: dict[str, Any],
    memory_cfg: Any,
) -> dict[str, Any]:
    """检索参数：key/topK 规范化后包成 ``{"param": ...}``。

    Raises:
        ValueError: keyword 为空或 topK 非法。
    """
    keyword = str(business.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("记忆检索需要非空的 keyword。")

    raw_top_k = business.get("topK", 0)
    try:
        top_k = int(raw_top_k or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"topK 必须是整数，收到 {raw_top_k!r}。") from exc
    if top_k < 0:
        raise ValueError("topK 必须大于等于 0。")
    if top_k == 0:
        top_k = memory_cfg.search_default_top_k
    top_k = min(
        top_k,
        memory_cfg.search_max_top_k,
        _PLATFORM_MAX_TOP_K,
    )
    return {"param": {"keyword": keyword, "topK": top_k}}


async def build_save_args(
    business: dict[str, Any],
    memory_cfg: Any,
) -> dict[str, Any]:
    """提取参数：``rounds`` → 查消息 → 组装 ``{"param": ...}``。

    Raises:
        ValueError: 无可提取消息 / rounds 非法。
    """
    user_id, session_id = _ctx_ids()
    if not user_id or not session_id:
        raise ValueError("记忆提取缺少会话上下文（user_id / session_id）。")
    if _storage is None:
        raise ValueError("记忆提取失败：存储未就绪。")

    raw_rounds = business.get("rounds", 0)
    try:
        rounds = int(raw_rounds or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rounds 必须是整数，收到 {raw_rounds!r}。") from exc
    if rounds < 0:
        raise ValueError("rounds 必须大于等于 0。")
    if rounds == 0:
        rounds = memory_cfg.extract_default_rounds
    rounds = min(rounds, memory_cfg.extract_max_rounds)

    messages = await _load_window(user_id, session_id, rounds)
    if not messages:
        raise ValueError("会话中暂无可提取的消息。")

    try:
        global_cfg = await get_memory_global_config()
        budget = int(global_cfg.max_tokens)
    except Exception:  # noqa: BLE001 —— 全局配置读失败走代码默认
        budget = MemoryGlobalConfig().max_tokens
    messages = _truncate_to_budget(messages, budget)

    payload_messages, _run_id = build_extract_payload(messages, rounds)
    if not payload_messages:
        raise ValueError("会话中暂无可提取的消息。")

    return {
        "param": {
            "memoryType": _MEMORY_TYPE,
            "inferFlag": _INFER_FLAG,
            "messages": payload_messages,
        },
    }


# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------
class MemoryMCPToolMiddleware(ToolMiddlewareBase):
    """记忆 MCP 工具的参数规范化中间件。"""

    async def on_tool_call(
        self,
        tool: Any,
        input_kwargs: dict[str, Any],
        next_handler: Any,
    ) -> AsyncGenerator[ToolChunk, None]:
        """按服务端工具名改写 arguments 后继续洋葱链。"""
        raw_tool = getattr(tool, "_tool", None)
        tool_name = str(getattr(raw_tool, "name", "") or "")
        business = input_kwargs.get("param")
        if not isinstance(business, dict):
            business = dict(input_kwargs)

        memory_cfg = get_app_config().memory
        try:
            if tool_name == memory_cfg.mcp_search_tool:
                arguments = normalize_search_args(business, memory_cfg)
            elif tool_name == memory_cfg.mcp_save_tool:
                arguments = await build_save_args(business, memory_cfg)
            else:
                arguments = input_kwargs
        except ValueError as exc:
            yield _error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 工具内异常不打断本轮
            logger.exception("memory mcp middleware failed: %s", tool_name)
            yield _error(f"记忆工具调用失败：{exc}")
            return

        async for chunk in next_handler(**arguments):
            yield chunk


__all__ = [
    "MemoryMCPToolMiddleware",
    "set_memory_tool_storage",
    "build_extract_payload",
    "normalize_search_args",
    "build_save_args",
]
