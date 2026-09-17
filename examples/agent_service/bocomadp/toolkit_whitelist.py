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
(PUT/DELETE ``/agents/{id}/tools/{name}``) stores **only** enterprise
tools and MCP names. All other tools (builtins / framework / project /
middleware) are always allowed.

Filter logic (reverse approach — only restrict what is explicitly
configurable):

- A tool whose name is **not** in the restricted set → always allowed
- A tool whose name **is** in the restricted set → only allowed if in
  the agent's whitelist

The restricted set = enterprise tool names + MCP names, populated once
at startup via :func:`set_restricted_tool_names`.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.toolkit_whitelist")

_original_get_toolkit: Any = None

_restricted_tool_names: set[str] = set()


def set_restricted_tool_names(names: set[str]) -> None:
    """Set the restricted tool names (enterprise + MCP) that require whitelist.

    Called once at startup from main.py. Only these names need whitelist
    authorization; everything else is always allowed.
    """
    global _restricted_tool_names
    _restricted_tool_names = names


def _keep_tool(tool: Any, whitelist: set[str]) -> bool:
    """Return whether *tool* should survive the filter.

    - Name not in restricted set → always allowed
    - Name in restricted set → allowed only if in *whitelist*
    """
    name = getattr(tool, "name", "")
    if name not in _restricted_tool_names:
        return True
    return name in whitelist


async def _whitelisted_get_toolkit(*args: Any, **kwargs: Any):
    """Assemble the toolkit, then filter by the per-agent whitelist."""
    toolkit = await _original_get_toolkit(*args, **kwargs)

    agent_record = kwargs.get("agent_record")
    agent_id = getattr(agent_record, "id", "") or ""

    from bocomadp.routers.agent_tools import _tool_whitelists

    whitelist = set(_tool_whitelists.get(agent_id, []))

    groups = getattr(toolkit, "tool_groups", None) or []
    for group in groups:
        group.tools = [t for t in group.tools if _keep_tool(t, whitelist)]
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


__all__ = ["patch_get_toolkit", "set_restricted_tool_names"]
