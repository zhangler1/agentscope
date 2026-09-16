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

The per-agent whitelist maintained by ``agent_tools_router``
(PUT/DELETE ``/agents/{id}/tools/{name}``) stores **only** enterprise
tools and MCP names. Default tools (builtins / framework / project)
are always allowed. Enterprise tools and MCPs must be explicitly added
to the whitelist to be available at runtime.

Filter logic:

- whitelist empty → only default tools survive (enterprise/MCP stripped)
- whitelist non-empty → default tools + whitelisted enterprise/MCP survive
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.toolkit_whitelist")

_original_get_toolkit: Any = None

_project_tool_names: set[str] = set()


def set_project_tool_names(names: set[str]) -> None:
    """Set the project tool names (called once at startup from main.py)."""
    global _project_tool_names
    _project_tool_names = names


_DEFAULT_TOOL_NAMES: set[str] = {
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "TeamCreate", "AgentCreate", "TeamSay", "TeamDelete", "AgentInvite",
    "TaskCreate", "TaskList", "TaskGet", "TaskUpdate",
    "ToolStop",
    "ScheduleCreate", "ScheduleDelete", "ScheduleList", "ScheduleUpdate",
}


def _always_allowed_names() -> set[str]:
    """Default tools + project tools — always allowed, never filtered."""
    return _DEFAULT_TOOL_NAMES | _project_tool_names


def _keep_default_or_whitelisted(tool: Any, always: set[str], allowed: set[str]) -> bool:
    """Return whether *tool* is always-allowed or in the whitelist."""
    name = getattr(tool, "name", "")
    if name in always:
        return True
    return name in allowed


async def _whitelisted_get_toolkit(*args: Any, **kwargs: Any):
    """Assemble the toolkit, then filter by the per-agent whitelist."""
    toolkit = await _original_get_toolkit(*args, **kwargs)

    agent_record = kwargs.get("agent_record")
    agent_id = getattr(agent_record, "id", "") or ""

    from bocomadp.routers.agent_tools import _tool_whitelists

    whitelist = _tool_whitelists.get(agent_id, [])
    allowed = set(whitelist)
    always = _always_allowed_names()

    groups = getattr(toolkit, "tool_groups", None) or []
    for group in groups:
        group.tools = [t for t in group.tools if _keep_default_or_whitelisted(t, always, allowed)]
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


__all__ = ["patch_get_toolkit", "set_project_tool_names"]
