# -*- coding: utf-8 -*-
"""记忆工具：记忆检索 / 记忆提取（HTTP 直连平台，普通 POST）。

两个工具作为**内置工具**由 :class:`ToolRegistry.load_custom_tools` 自动
扫描注册，所有智能体均可见（无开关），与 ``bocomadp.memory`` 的自动
记忆链路（中间件检索注入 / 轮数触发提取）并存。

调用形态与项目根 ``记忆 mcp.txt`` 一致：直接 POST 一份 JSON-RPC
``tools/call`` 报文（不是 MCP 客户端），响应取 ``structuredContent``。
平台侧 ``agentId`` 使用本地 ``agent_id``，``agentPlat`` / ``memoryType`` /
``inferFlag`` 使用示例固定值（0 / 1 / 1）。

安全约定：``user_id`` / ``session_id`` / ``agent_id`` **不作为工具参数暴露**
（防模型伪造越权），统一从 ContextVar + 会话记录反查。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from bocomadp.config import get_app_config
from bocomadp.memory.extractor import (
    _load_recent_messages,
    _truncate_to_budget,
)
from bocomadp.memory.global_config_store import get_memory_global_config
from bocomadp.memory.platform_http import MemoryHttpError, call_memory_tool

from .._naming import tool_name

try:
    from agentscope.tool import tool
except ImportError:  # pragma: no cover - 未安装 agentscope 时的语法检查兜底

    def tool(*args: Any, **kwargs: Any):  # type: ignore
        """Fallback ``@tool`` decorator."""

        if len(args) == 1 and callable(args[0]):
            fn = args[0]
            fn._is_tool = True  # type: ignore
            return fn

        def decorator(fn: Any) -> Any:
            fn._is_tool = True  # type: ignore
            return fn

        return decorator


logger = logging.getLogger("as")

#: 框架 storage（main.py 经 :func:`set_memory_tool_storage` 注入一次）。
_storage: Any = None

#: 提取窗口最多回溯的消息条数（防止超长会话一次拉全量）。
_MAX_WINDOW_MESSAGES = 800

#: 平台固定业务参数（示例请求取值，本次不做配置化）。
_MEMORY_TYPE = 1
_INFER_FLAG = 1
_AGENT_PLAT = "0"


def set_memory_tool_storage(storage: Any) -> None:
    """注入框架 storage（main.py 启动时调用一次）。"""
    global _storage
    _storage = storage


# ---------------------------------------------------------------------------
# 上下文与请求构造
# ---------------------------------------------------------------------------
def _ctx_ids() -> tuple[str, str]:
    """当前请求的 ``(user_id, session_id)``（build_agent_tools 注入）。"""
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_user_id,
    )

    return (
        (_current_user_id.get() or "").strip(),
        (_current_session_id.get() or "").strip(),
    )


def _ctx_token() -> str:
    """当前请求的 ``guwp-token``（ASGI 层从 ``guwpToken`` 请求头捕获）。"""
    from bocomadp.tools.agent_factory_tools import _current_token

    return (_current_token.get() or "").strip()


def _ctx_run_id() -> str:
    """当前 chat run 的 id（提取接口的 ``runId`` 回退值）。"""
    from bocomadp.logging.trace_context import get_current_run_id

    return (get_current_run_id() or "").strip()


async def _resolve_agent_id(user_id: str, session_id: str) -> str:
    """按会话记录反查 ``agent_id``（``get_session`` 只按 user+session 定位）。"""
    if _storage is None:
        return ""
    try:
        session = await _storage.get_session(user_id, "", session_id)
    except Exception:  # noqa: BLE001 —— 查询失败按缺失处理
        logger.exception("memory tool: resolve agent_id failed")
        return ""
    return str(getattr(session, "agent_id", "") or "")


def _headers(memory_cfg: Any, agent_id: str) -> dict[str, str]:
    """平台请求头（agent-key / agentId / agentPlat / guwp-token）。"""
    return {
        "agent-key": memory_cfg.mcp_agent_key,
        "agentId": agent_id,
        "agentPlat": _AGENT_PLAT,
        "mcp-protocol-version": memory_cfg.mcp_protocol_version,
        "guwp-token": _ctx_token(),
    }


# ---------------------------------------------------------------------------
# 消息窗口：过滤 / 分轮 / 预算裁剪
# ---------------------------------------------------------------------------
def _text_of(msg: Any) -> str:
    """消息文本（空消息返回空串）。"""
    try:
        return (msg.get_text_content() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_extract_payload(
    messages: list[Any],
    rounds: int,
) -> tuple[list[dict[str, str]], str]:
    """构造提取请求的 ``messages`` 列表与 ``runId``。

    规则：

    - 只保留 ``role ∈ {user, assistant}`` 且文本非空的消息；
    - 以最后一条 assistant 为锚，向前按 assistant 切块（块内含该轮全部
      连续 user，允许多条 user），取最后 ``rounds`` 块；
    - 末尾追加“本轮 user 消息”（最后一条 assistant 之后的所有 user）；
    - ``runId`` = 最后一块末条 assistant 的 id；无块时回退本轮 user 的 id。

    Args:
        messages: 时间正序的 ``Msg`` 列表。
        rounds: 回溯轮数（>=1）。

    Returns:
        ``(payload, run_id)``；无任何有效消息时 payload 为空列表、
        run_id 为空串。
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
        # 首次对话：没有任何 assistant，全部 user 都属于“本轮及之前”。
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
    """读取提取窗口（最近一批消息，正序）。"""
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
# 工具：记忆检索
# ---------------------------------------------------------------------------
@tool
async def memory_search(keyword: str, topK: int = 0) -> str:
    """检索用户的长期记忆（偏好、事实、历史约定等）。

    何时使用：
    - 用户询问“我之前说过”“你还记得”“按我的习惯”等需要回忆长期信息时；
    - 回答前需要确认用户既有偏好 / 背景事实时。

    何时不要使用：
    - 当前对话上下文已经包含所需信息时；
    - 与本轮任务无关的闲聊。

    Args:
        keyword (str): 检索关键词，贴近用户原话的核心主题。
        topK (int): 返回条数；不传或传 0 使用系统默认值，超过上限自动截断。

    Returns:
        str: JSON 字符串
            ``{"memories": [{"memory":..., "score":..., "createdAt":...}], "count": n}``；
            失败时返回可读错误文本。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return "记忆检索需要非空的 keyword。"

    memory_cfg = get_app_config().memory
    if not memory_cfg.mcp_search_url:
        return "记忆检索未配置：memory.mcp_search_url 为空。"

    user_id, session_id = _ctx_ids()
    if not user_id or not session_id:
        return "记忆检索缺少会话上下文（user_id / session_id）。"
    token = _ctx_token()
    if not token:
        return "记忆检索缺少用户凭证（guwpToken），无法访问记忆服务。"

    limit = int(topK or 0)
    if limit < 0:
        return "topK 必须大于等于 0。"
    if limit == 0:
        limit = memory_cfg.search_default_top_k
    limit = min(limit, memory_cfg.search_max_top_k)

    agent_id = await _resolve_agent_id(user_id, session_id)
    if not agent_id:
        return "记忆检索失败：无法定位当前会话的 agent_id。"

    try:
        payload = await call_memory_tool(
            url=memory_cfg.mcp_search_url,
            tool=memory_cfg.mcp_search_tool,
            arguments={"param": {"keyword": keyword, "topK": limit}},
            headers=_headers(memory_cfg, agent_id),
            timeout=memory_cfg.mcp_timeout_seconds,
        )
    except MemoryHttpError as exc:
        logger.warning("memory search failed session=%s: %s", session_id, exc)
        return f"记忆检索失败：{exc}"

    raw_items = payload.get("result")
    items = raw_items if isinstance(raw_items, list) else []
    memories = [
        {
            "memory": str(item.get("memory") or ""),
            "score": item.get("score"),
            "createdAt": item.get("createdAt"),
        }
        for item in items
        if isinstance(item, dict) and item.get("memory")
    ]
    logger.info(
        "memory search session=%s keyword=%r topK=%d hits=%d",
        session_id,
        keyword,
        limit,
        len(memories),
    )
    return json.dumps(
        {"memories": memories, "count": len(memories)},
        ensure_ascii=False,
    )


memory_search._tool_display_name = tool_name("记忆检索", "memory_search")


# ---------------------------------------------------------------------------
# 工具：记忆提取
# ---------------------------------------------------------------------------
@tool
async def memory_extract(rounds: int = 0) -> str:
    """从最近若干轮对话中提取并保存用户记忆。

    何时使用：
    - 用户透露了值得长期记住的偏好 / 事实 / 约定后；
    - 一段任务完成、用户明确表示“记一下”类信息时。

    何时不要使用：
    - 没有新的用户消息（本轮无输入）时；
    - 纯闲聊、无长期价值的信息。

    Args:
        rounds (int): 回溯对话轮数（1 ~ 配置上限）；不传或传 0 使用系统默认值。

    Returns:
        str: JSON 字符串
            ``{"ok": true, "rounds": n, "messages": m, "result": "success"}``；
            失败或无内容时返回可读错误文本。
    """
    memory_cfg = get_app_config().memory
    if not memory_cfg.mcp_save_url:
        return "记忆提取未配置：memory.mcp_save_url 为空。"

    user_id, session_id = _ctx_ids()
    if not user_id or not session_id:
        return "记忆提取缺少会话上下文（user_id / session_id）。"
    token = _ctx_token()
    if not token:
        return "记忆提取缺少用户凭证（guwpToken），无法访问记忆服务。"
    if _storage is None:
        return "记忆提取失败：存储未就绪。"

    limit = int(rounds or 0)
    if limit < 0:
        return "rounds 必须大于等于 0。"
    if limit == 0:
        limit = memory_cfg.extract_default_rounds
    limit = min(limit, memory_cfg.extract_max_rounds)

    agent_id = await _resolve_agent_id(user_id, session_id)
    if not agent_id:
        return "记忆提取失败：无法定位当前会话的 agent_id。"

    try:
        messages = await _load_window(user_id, session_id, limit)
    except Exception as exc:  # noqa: BLE001 —— 读库失败降级为可读错误
        logger.exception("memory extract: load window failed")
        return f"记忆提取失败：读取会话消息出错（{exc}）。"
    if not messages:
        return "会话中暂无可提取的消息。"

    try:
        global_cfg = await get_memory_global_config()
        budget = int(global_cfg.max_tokens)
    except Exception:  # noqa: BLE001 —— 全局配置读失败走代码默认
        from bocomadp.memory.config import MemoryGlobalConfig

        budget = MemoryGlobalConfig().max_tokens
    messages = _truncate_to_budget(messages, budget)

    payload_messages, last_msg_id = build_extract_payload(messages, limit)
    if not payload_messages:
        return "会话中暂无可提取的消息。"

    run_id = last_msg_id or _ctx_run_id()
    try:
        payload = await call_memory_tool(
            url=memory_cfg.mcp_save_url,
            tool=memory_cfg.mcp_save_tool,
            arguments={
                "param": {
                    "memoryType": _MEMORY_TYPE,
                    "inferFlag": _INFER_FLAG,
                    "messages": payload_messages,
                },
            },
            headers={**_headers(memory_cfg, agent_id), "runId": run_id},
            timeout=memory_cfg.mcp_timeout_seconds,
        )
    except MemoryHttpError as exc:
        logger.warning("memory extract failed session=%s: %s", session_id, exc)
        return f"记忆提取失败：{exc}"

    result = payload.get("result")
    logger.info(
        "memory extract session=%s rounds=%d messages=%d result=%r",
        session_id,
        limit,
        len(payload_messages),
        result,
    )
    if result != "success":
        return f"记忆提取失败：平台返回 {result!r}。"
    return json.dumps(
        {
            "ok": True,
            "rounds": limit,
            "messages": len(payload_messages),
            "result": "success",
        },
        ensure_ascii=False,
    )


memory_extract._tool_display_name = tool_name("记忆提取", "memory_extract")


__all__ = [
    "set_memory_tool_storage",
    "build_extract_payload",
    "memory_search",
    "memory_extract",
]
