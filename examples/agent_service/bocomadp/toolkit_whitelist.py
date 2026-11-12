# -*- coding: utf-8 -*-
"""Per-agent tool whitelist applied at the ``Toolkit`` level.

The framework's ``get_toolkit`` attaches many tool sources besides the
caller-supplied ``extra_factory``:

- workspace builtins (Bash / Read / Write / Edit / Glob / Grep)
- planning tools (TaskCreate / TaskList / TaskGet / TaskUpdate)
- background-task control (ToolStop)
- schedule control (ScheduleCreate / ...)
- team tools (TeamCreate / AgentCreate / TeamSay / TeamDelete /
  AgentInvite)
- middleware-provided tools
- project tools (from ToolRegistry)
- enterprise tools (from build_enterprise_tools)

The per-agent whitelist maintained by ``agent_tools_router``
(PUT/DELETE ``/agents/{id}/tools/{name}``) stores enterprise tools
and MCP names. Enterprise tools and MCPs must be explicitly listed
in the whitelist or ``usableTools`` to survive; default tools
(builtins / framework / project) are always allowed.

Filter logic (aligned with ``build_agent_tools`` in main.py):

- name not in restricted set → always allowed
- name in restricted set → allowed if in whitelist OR usableTools
"""

from __future__ import annotations

import logging
from typing import Any

from bocomadp.mcp import MEMORY_MCP_NAME

logger = logging.getLogger("bocomadp.toolkit_whitelist")

#: 平台 schema 对 topK 的硬上限。
_PLATFORM_MAX_TOP_K = 20


def _memory_search_schema(memory_cfg: Any) -> dict[str, Any]:
    """检索工具展示 schema：平铺 keyword / topK（隐藏 param 包装）。"""
    max_top_k = min(memory_cfg.search_max_top_k, _PLATFORM_MAX_TOP_K)
    default_top_k = min(memory_cfg.search_default_top_k, max_top_k)
    return {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "title": "检索词",
                "description": "检索关键词，贴近用户原话的核心主题。",
            },
            "topK": {
                "type": "integer",
                "title": "返回条数",
                "description": "返回的记忆条数；不传使用系统默认值。",
                "default": default_top_k,
                "minimum": 1,
                "maximum": max_top_k,
            },
        },
        "required": ["keyword"],
    }


def _memory_save_schema(memory_cfg: Any) -> dict[str, Any]:
    """提取工具展示 schema：只暴露对话轮数。"""
    max_rounds = memory_cfg.extract_max_rounds
    default_rounds = min(memory_cfg.extract_default_rounds, max_rounds)
    return {
        "type": "object",
        "properties": {
            "rounds": {
                "type": "integer",
                "title": "对话轮数",
                "description": (
                    "回溯最近多少轮对话用于提取记忆；"
                    "不传使用系统默认值，超出上限自动截断。"
                ),
                "default": default_rounds,
                "minimum": 1,
                "maximum": max_rounds,
            },
        },
        "required": [],
    }


def _decorate_memory_tool(tool: Any, memory_cfg: Any) -> None:
    """改写记忆 MCP 工具的 schema / 权限，并挂参数中间件。

    工具名保持 MCP 原名（``mcp__euvd-memory__EUVDxxx``）；底层
    ``MCPTool.call`` 用的是服务端原始名，改名与否不影响调用。
    """
    from bocomadp.memory.mcp_tool_middleware import (
        MemoryMCPToolMiddleware,
    )

    raw_tool = getattr(tool, "_tool", None)
    server_name = str(getattr(raw_tool, "name", "") or "")
    if server_name == memory_cfg.mcp_search_tool:
        tool.input_schema = _memory_search_schema(memory_cfg)
    elif server_name == memory_cfg.mcp_save_tool:
        tool.input_schema = _memory_save_schema(memory_cfg)
    else:
        return

    middlewares = getattr(tool, "_middlewares", None)
    if middlewares is None:
        middlewares = []
        tool._middlewares = middlewares
    if not any(
        isinstance(mw, MemoryMCPToolMiddleware) for mw in middlewares
    ):
        middlewares.append(MemoryMCPToolMiddleware())

    # 记忆工具不需要人工确认（平台 readOnly 字段非标准，框架读不到）。
    tool.is_read_only = True


#: MCP 注册表（main.py 启动时注入），用于取原始 client 绕过沙箱 gateway。
_mcp_registry: Any = None


def set_mcp_registry(registry: Any) -> None:
    """注入 MCP 注册表（main.py 启动时调用一次）。"""
    global _mcp_registry
    _mcp_registry = registry


def _registry_mcp(name: str) -> Any:
    """按名称取注册表里的原始 MCPClient（沙箱模式下带动态头逻辑）。"""
    if _mcp_registry is None:
        return None
    try:
        for mcp in _mcp_registry.list_mcps():
            if getattr(mcp, "name", "") == name:
                return mcp
    except Exception:  # noqa: BLE001 —— 注册表异常不影响对话
        logger.warning("memory mcp: registry lookup failed", exc_info=True)
    return None


async def _materialize_memory_mcp(toolkit: Any) -> None:
    """把记忆 MCP 的工具展开到 ``group.tools`` 并做展示层装饰。

    框架的 ``Toolkit._get_available_tools`` 每轮会自己 ``list_tools``；
    这里提前展开并清空 ``group.mcps``，避免同一轮重复请求。失败时保留
    client，交给框架原有兜底（list 失败只跳过该 MCP）。

    **沙箱模式下** workspace 里的 client 是 ``GatewayMCPClient``，其请求
    由沙箱内 gateway 转发、只带注册时的静态 headers，每请求动态注入的
    ``guwp-token / agentId / runId`` 会丢失。因此这里优先换成注册表里的
    原始 ``MemoryMCPClient``（应用进程内直连记忆平台），保证发现与执行
    两个阶段都能带上动态头。
    """
    from bocomadp.config import get_app_config

    memory_cfg = get_app_config().memory
    for group in getattr(toolkit, "tool_groups", None) or []:
        clients = getattr(group, "mcps", None) or []
        remaining = []
        for client in clients:
            name = str(getattr(client, "name", "") or "")
            if name != MEMORY_MCP_NAME:
                remaining.append(client)
                continue
            original = _registry_mcp(name)
            if original is not None and original is not client:
                logger.info(
                    "memory mcp: using registry client for %r "
                    "(bypass sandbox gateway)",
                    name,
                )
                client = original
            try:
                tools = await client.list_tools()
                logger.info(
                    "memory mcp: %s list_tools -> %d tools",
                    name,
                    len(tools),
                )
                if not tools:
                    logger.warning(
                        "memory mcp: %s returned 0 tools "
                        "(check guwp-token / agentId headers)",
                        name,
                    )
            except Exception:  # noqa: BLE001 —— 记忆服务不可用不阻断对话
                logger.warning(
                    "memory mcp: list_tools failed for %s; "
                    "falling back to framework handling",
                    name,
                    exc_info=True,
                )
                remaining.append(client)
                continue
            for tool in tools:
                _decorate_memory_tool(tool, memory_cfg)
                group.tools.append(tool)
        group.mcps = remaining

_original_get_toolkit: Any = None

_restricted_tool_names: set[str] = set()


def set_restricted_tool_names(names: set[str]) -> None:
    """Set the restricted tool names (enterprise + MCP) that require whitelist.

    Called once at startup from main.py. Only these names need whitelist
    authorization; everything else is always allowed.
    """
    global _restricted_tool_names
    _restricted_tool_names = names


def _keep_tool(tool: Any, allowed: set[str]) -> bool:
    """Return whether *tool* should survive the filter.

    - Name not in restricted set → always allowed
    - Name in restricted set → allowed only if in *allowed*
    """
    name = getattr(tool, "name", "")
    if name not in _restricted_tool_names:
        return True
    return name in allowed


async def _whitelisted_get_toolkit(*args: Any, **kwargs: Any):
    """Assemble the toolkit, then filter by the per-agent whitelist.

    Semantics aligned with ``build_agent_tools`` (main.py):

    - name not in restricted set → always allowed (builtins / framework / project)
    - name in restricted set → allowed if in whitelist OR usableTools
    - whitelist empty + usableTools empty → restricted tools all removed
    """
    toolkit = await _original_get_toolkit(*args, **kwargs)

    # 记忆 MCP：提前展开工具（改写 schema / 挂参数中间件 / 免确认），
    # 并清空对应 client，避免框架本轮再 list 一次。
    try:
        await _materialize_memory_mcp(toolkit)
    except Exception:  # noqa: BLE001 —— 记忆工具缺席不阻断对话
        logger.exception("memory mcp: materialize failed")

    agent_record = kwargs.get("agent_record")
    agent_id = getattr(agent_record, "id", "") or ""

    from bocomadp.routers.agent_tools import _tool_whitelists
    from bocomadp.tools.enterprise import usable_enterprise_tool_names
    from bocomadp.deerflow.custom_params import get_custom_params

    whitelist = _tool_whitelists.get(agent_id, [])
    usable = get_custom_params().get("usableTools")
    allowed = set(whitelist) | usable_enterprise_tool_names(usable)

    groups = getattr(toolkit, "tool_groups", None) or []
    for group in groups:
        group.tools = [t for t in group.tools if _keep_tool(t, allowed)]
    toolkit.tool_groups = [
        g
        for g in groups
        if g.name == "basic" or g.tools or getattr(g, "mcps", None)
    ]
    return toolkit


def patch_get_toolkit() -> None:
    """Replace the chat service's ``get_toolkit`` binding (idempotent).

    Must run before the first chat run; the wrapper is looked up at
    call time via the module global, so there is no import-order race.
    """
    global _original_get_toolkit
    if _original_get_toolkit is not None:
        return

    from agentscope.app._service import _chat as _chat_module

    _original_get_toolkit = _chat_module.get_toolkit
    _chat_module.get_toolkit = _whitelisted_get_toolkit
    logger.info(
        "patched %s.get_toolkit with per-agent whitelist filter",
        _chat_module.__name__,
    )


def _keep(tool: Any, allowed: set[str]) -> bool:
    """Legacy name-based keep predicate (kept for older callers/tests)."""
    return getattr(tool, "name", "") in allowed


__all__ = ["patch_get_toolkit", "set_restricted_tool_names"]
