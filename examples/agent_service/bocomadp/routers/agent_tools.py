# -*- coding: utf-8 -*-
"""Agent tool management — query & toggle per-agent tool enablement.

Endpoints
---------
``GET    /tools``                              — list all tools (global)
``GET    /mcps``                               — list all MCP servers (global)
``GET    /search``                             — search tools or MCPs by name
``GET    /agents/{agent_id}/tools``           — list agent's tools with status
``PUT    /agents/{agent_id}/tools?tool_name=...``   — add a tool/MCP to agent
``DELETE /agents/{agent_id}/tools?tool_name=...``   — remove a tool/MCP from agent

Tool categories
---------------
**Default tools** (always available, not shown, not configurable):

- Builtins: ``Bash/Read/Write/Edit/Glob/Grep``
- Framework: ``Team*/Task*``
- Project: from ``ToolRegistry`` (builtin_tools.py + custom/)

**Configurable tools** (must be explicitly added to use & show):

- Enterprise tools — built by ``build_enterprise_tools``
- MCP servers — from ``McpRegistry``

Whitelist semantics
-------------------
The whitelist stores enterprise tools and MCP names.
- ``whitelist == []`` → only default tools available (enterprise/MCP disabled)
- ``whitelist == ["通讯录查询", "browser-use"]`` → default + listed tools/MCPs available

At runtime (``build_agent_tools`` / ``toolkit_whitelist``), enterprise tools
can also be enabled via ``usableTools`` (request-level override), which
bypasses the whitelist. This is for agents that cannot call the PUT API
themselves (the caller injects usableTools on their behalf).

Users browse candidates via ``GET /tools`` & ``GET /mcps``, then add/remove
via ``PUT/DELETE /agents/{id}/tools/{name}``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from agentscope.app.deps import get_current_user_id

from ..tool_catalog import (
    AGENT_CREATOR_ID,
    BUILTIN_TOOLS_META,
    FRAMEWORK_TOOLS_META,
    canonical_tool_name,
)

logger = logging.getLogger("bocomadp.agent_tools")

agent_tools_router = APIRouter(
    prefix="/agents",
    tags=["agent-tools"],
)

catalog_router = APIRouter(
    tags=["tool-catalog"],
)

# ------------------------------------------------------------------
# workspace builtins — 名称/描述统一取自 bocomadp.tool_catalog
# ------------------------------------------------------------------
#: 注意名字是**运行时真值**（首字母大写）。历史上这里用的是小写名，
#: 与运行时 ``Bash`` 等对不上，导致白名单对 builtins 失效。
_BUILTIN_TOOLS: list[dict] = [dict(m) for m in BUILTIN_TOOLS_META]

#: 团队/规划工具的静态元数据（name + 简短 description）。
#: 默认工具（始终可用，不显示，不可配置）。
_FRAMEWORK_TOOLS_META: list[dict] = [dict(m) for m in FRAMEWORK_TOOLS_META]

#: 智能体工厂自带的工厂工具（仅供 ``_agent-creator`` 的 GET 展示，不可配置）。
_FACTORY_TOOL_ATTRS: tuple[str, ...] = (
    "create_agent",
    "update_agent",
    "delete_agent",
    "list_agents",
    "get_agent",
    "get_agent_tools",
    "list_tools_for_agent",
    "set_agent_tools",
    "list_available_skills",
    "enable_skill_for_agent",
)

_HIDDEN_PROJECT_TOOLS: frozenset[str] = frozenset(
    {"回显", "获取当前时间", "列出上传文件", "读取上传文件",
     "echo", "get_current_time", "list_uploaded_files", "read_uploaded_file"},
)

# ------------------------------------------------------------------
# Tool whitelist store
# ------------------------------------------------------------------
# Framework agents (StorageBase) have no ``enabled_tools`` field.
# This dict acts as the write target for tool enable / disable so
# the tool config APIs work for every framework-managed agent.
_tool_whitelists: dict[str, list[str]] = {}


def _whitelist_file() -> Path:
    """Persistent storage path for the tool whitelist store.

    Lives under ``{workspace_dir}/_meta/`` so it survives service
    restarts (the store itself is in-memory and would otherwise be
    lost, silently re-granting every tool to whitelisted agents).
    """
    try:
        from bocomadp.config.uploads_config import get_workspace_dir

        return get_workspace_dir() / "_meta" / "agent_tool_whitelists.json"
    except Exception:  # noqa: BLE001
        return Path(
            os.environ.get(
                "BOCMADP_WHITELIST_FILE",
                os.path.join(
                    os.path.dirname(os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__)))),
                    ".agent_tool_whitelists.json",
                ),
            )
        )


def _persist_whitelists() -> None:
    """Write the whitelist store to disk (best effort)."""
    path = _whitelist_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_tool_whitelists, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        logger.warning("persist tool whitelists failed", exc_info=True)


def load_tool_whitelists() -> None:
    """Restore the whitelist store from disk (called at startup)."""
    path = _whitelist_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("whitelist file %s is not a dict; ignoring", path)
            return
        _tool_whitelists.clear()
        for aid, names in data.items():
            if not isinstance(names, list):
                logger.warning(
                    "whitelist for %s is not a list; skipped",
                    aid,
                )
                continue
            # 归一化历史小写 builtin 名（bash → Bash 等），其余名字保留：
            # 智能体工厂的白名单含工厂工具名，它们不在 M 内，需原样保留。
            _tool_whitelists[aid] = [
                canonical_tool_name(str(n)) for n in names
            ]
        logger.info(
            "loaded %d agent tool whitelists from %s",
            len(_tool_whitelists),
            path,
        )
    except FileNotFoundError:
        logger.info("no tool whitelist file yet: %s", path)
    except Exception:  # noqa: BLE001
        logger.warning("load tool whitelists failed", exc_info=True)


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _tool_registry(request: Request):
    """Return the global :class:`ToolRegistry` from app state."""
    return request.app.state.tool_registry


def _mcp_registry(request: Request):
    """Return the global :class:`McpRegistry` from app state (may be None)."""
    return getattr(request.app.state, "mcp_registry", None)


async def _resolve_framework_agent(
    request: Request,
    user_id: str,
    agent_id: str,
) -> Any:
    """Look up *agent_id* in framework StorageBase.

    Scoped by the authenticated caller *user_id* so the tool config
    APIs work for every user, not just ``default``.

    Returns:
        The :class:`AgentRecord` if found, ``None`` otherwise.
    """
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        return None
    try:
        return await storage.get_agent(user_id, agent_id)
    except Exception:
        logger.debug(
            "agent_tools: storage lookup failed for %s",
            agent_id,
            exc_info=True,
        )
        return None


def _get_enabled_tools(agent_id: str) -> list[str]:
    """Return the current enabled-tools whitelist for *agent_id*.

    The whitelist contains **only** enterprise tool and MCP names.
    Default tools (builtins / framework / project) are always available
    and never appear in the whitelist.
    """
    return list(_tool_whitelists.get(agent_id, []))


def _set_enabled_tools(agent_id: str, tools: list[str]) -> None:
    """Persist *tools* for *agent_id*."""
    _tool_whitelists[agent_id] = tools
    _persist_whitelists()


def _configurable_tool_names(request: Request) -> set[str]:
    """Enterprise tools + MCP names — the set that can be added/removed.

    Default tools (builtins / framework / project) are always present
    at runtime and are **not** part of this set.
    """
    names: set[str] = set()
    mcp_reg = _mcp_registry(request)
    if mcp_reg is not None:
        for mcp in mcp_reg.list_mcps():
            name = getattr(mcp, "name", "") or ""
            if name:
                names.add(name)
    names.update(m["name"] for m in _enterprise_tools_meta())
    return names


def _enterprise_tools_meta() -> list[dict]:
    """企业工具元数据（延迟导入，避免请求层导入重量级工具模块）。

    导入失败时降级为空列表，不影响其余工具目录。
    """
    try:
        from ..tools.enterprise_catalog import enterprise_tools_meta
    except Exception:  # noqa: BLE001 —— 企业工具不可用时降级
        logger.warning("enterprise tools unavailable; skipped", exc_info=True)
        return []
    return enterprise_tools_meta()


def _factory_tools_meta() -> list[dict]:
    """智能体工厂自带工厂工具的元数据（仅用于 ``_agent-creator`` 展示）。

    这些工具由 ``build_agent_tools`` 在运行时按 agent_id 注入，**不在**
    可配置集合 M 内，因此不参与校验/启停；只在 ``GET`` 响应里展示，
    让工具面板能完整反映智能体工厂的实际能力。
    """
    try:
        from ..tools import agent_factory_tools as _aft
    except Exception:  # noqa: BLE001 —— 工厂工具不可用时降级
        logger.debug("factory tools unavailable; skipped", exc_info=True)
        return []

    metas: list[dict] = []
    for attr in _FACTORY_TOOL_ATTRS:
        tool = getattr(_aft, attr, None)
        name = getattr(tool, "name", "") or ""
        if not name:
            continue
        metas.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
            },
        )
    return metas


# ------------------------------------------------------------------
# GET /agents/{agent_id}/tools
# ------------------------------------------------------------------


@agent_tools_router.get(
    "/{agent_id}/tools",
    summary="List tools with per-agent enablement status",
)
async def list_agent_tools(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Return the agent's tools: default (always on) + authorized (whitelist).

    - Project tools: always shown, ``toggleable=False``
    - Enterprise tools / MCPs: only shown when in the whitelist,
      ``toggleable=True``

    Builtins and framework tools are never shown.

    Response::

        {
          "agent_id": "...",
          "tools": [
            {"name": "echo", "description": "...", "enabled": true, "toggleable": false},
            {"name": "通讯录查询", "description": "...", "enabled": true, "toggleable": true}
          ],
          "mcps": [
            {"name": "browser-use", "description": "...", "enabled": true, "toggleable": true}
          ]
        }
    """
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    whitelist = set(_get_enabled_tools(agent_id))

    tools: list[dict] = []
    mcps: list[dict] = []

    # 1. 项目工具（始终可用，不可配置）
    for tool in _tool_registry(request).list_tools():
        name = _tool_name(tool)
        tools.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
                "enabled": True,
                "toggleable": False,
            },
        )

    # 2. 企业工具（仅在白名单中才显示）
    for meta in _enterprise_tools_meta():
        name = meta["name"]
        if name not in whitelist:
            continue
        tools.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "enabled": True,
                "toggleable": True,
            },
        )

    # 3. MCP servers（仅在白名单中才显示）
    mcp_reg = _mcp_registry(request)
    if mcp_reg is not None:
        for mcp in mcp_reg.list_mcps():
            mcp_name = getattr(mcp, "name", "") or ""
            if mcp_name not in whitelist:
                continue
            mcps.append(
                {
                    "name": mcp_name,
                    "description": (
                        getattr(mcp, "description", None)
                        or getattr(
                            getattr(mcp, "mcp_config", None),
                            "url",
                            "",
                        )
                        or ""
                    ),
                    "enabled": True,
                    "toggleable": True,
                },
            )

    return {
        "agent_id": agent_id,
        "tools": tools,
        "mcps": mcps,
    }


# ------------------------------------------------------------------
# PUT /agents/{agent_id}/tools/{tool_name}   — enable
# ------------------------------------------------------------------


def _resolve_configurable_name(
    raw_name: str,
    configurable: set[str],
) -> str | None:
    """Resolve *raw_name* to a name in *configurable*.

    Accepts both Chinese and English tool names (e.g. ``"利率查询"`` or
    ``"interest_rate"``) and returns the actual runtime name present in
    *configurable*.  Returns ``None`` if no match.
    """
    if raw_name in configurable:
        return raw_name
    from ..tools.enterprise import _NAME_TO_CANONICAL, _CANONICAL_TO_CN
    from ..tools._naming import tool_name as _tool_name

    canonical = _NAME_TO_CANONICAL.get(raw_name)
    if canonical:
        resolved = _tool_name(_CANONICAL_TO_CN[canonical], canonical)
        if resolved in configurable:
            return resolved
    return None


def _is_agent_specific_tool(resolved_name: str) -> bool:
    """Check whether *resolved_name* is an agent-specific enterprise tool.

    Agent-specific tools (contact_search, physical_contact_search, etc.)
    are controlled by ``usableTools`` at request level — they cannot be
    enabled via the per-agent whitelist API.
    """
    from ..tools.enterprise import _NAME_TO_CANONICAL, _AGENT_SPECIFIC_CANONICAL
    canonical = _NAME_TO_CANONICAL.get(resolved_name)
    if canonical and canonical in _AGENT_SPECIFIC_CANONICAL:
        return True
    return False


@agent_tools_router.put(
    "/{agent_id}/tools",
    summary="Add a tool or MCP to the agent",
)
async def enable_agent_tool(
    agent_id: str,
    tool_name: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Add *tool_name* (enterprise tool or MCP) to the agent's whitelist.

    Only enterprise tools and MCP names are accepted — default tools
    (builtins / framework / project) are always available and cannot be
    toggled.  Both Chinese and English names are accepted (e.g.
    ``"利率查询"`` and ``"interest_rate"``).
    """
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    configurable = _configurable_tool_names(request)
    resolved = _resolve_configurable_name(tool_name, configurable)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found or not configurable",
        )

    if _is_agent_specific_tool(resolved):
        raise HTTPException(
            status_code=403,
            detail="此智能体无使用权限",
        )

    current = _get_enabled_tools(agent_id)

    if resolved in current:
        logger.info("agent_tools: %s add %s (already in whitelist)", agent_id, resolved)
        return {"ok": True}

    current.append(resolved)
    _set_enabled_tools(agent_id, current)
    logger.info("agent_tools: %s add %s → whitelist=%s", agent_id, resolved, current)
    return {"ok": True}


# ------------------------------------------------------------------
# DELETE /agents/{agent_id}/tools/{tool_name}   — disable
# ------------------------------------------------------------------


@agent_tools_router.delete(
    "/{agent_id}/tools",
    summary="Remove a tool or MCP from the agent",
)
async def disable_agent_tool(
    agent_id: str,
    tool_name: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Remove *tool_name* from the agent's whitelist.

    The tool/MCP will no longer be available or shown for this agent.
    Default tools (builtins / framework / project) cannot be removed.
    Both Chinese and English names are accepted.
    """
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    configurable = _configurable_tool_names(request)
    resolved = _resolve_configurable_name(tool_name, configurable)
    if resolved is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found or not configurable",
        )

    current = _get_enabled_tools(agent_id)

    if resolved not in current:
        logger.info("agent_tools: %s remove %s (not in whitelist)", agent_id, resolved)
        return {"ok": True}

    current.remove(resolved)
    _set_enabled_tools(agent_id, current)
    logger.info("agent_tools: %s remove %s → whitelist=%s", agent_id, resolved, current)
    return {"ok": True}


# ------------------------------------------------------------------
# GET /tools  — list all tools (global, not per-agent)
# ------------------------------------------------------------------


@catalog_router.get(
    "/tools",
    summary="List all available tools (global)",
)
async def list_all_tools(request: Request) -> dict:
    """Return every tool across project + enterprise sources (excludes builtins).

    Response::

        {
          "tools": [
            {"name": "echo", "description": "..."},
            {"name": "通讯录查询", "description": "..."}
          ]
        }
    """
    tools: list[dict] = []

    for tool in _tool_registry(request).list_tools():
        name = _tool_name(tool)
        if name in _HIDDEN_PROJECT_TOOLS:
            continue
        tools.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
            },
        )

    for meta in _enterprise_tools_meta():
        tools.append(
            {
                "name": meta["name"],
                "description": meta.get("description", ""),
            },
        )

    return {"tools": tools}


# ------------------------------------------------------------------
# GET /mcps  — list all MCP servers (global, not per-agent)
# ------------------------------------------------------------------


@catalog_router.get(
    "/mcps",
    summary="List all MCP servers (global)",
)
async def list_all_mcps(request: Request) -> dict:
    """Return every registered MCP server.

    Response::

        {
          "mcps": [
            {"name": "browser-use", "description": "..."}
          ]
        }
    """
    mcps: list[dict] = []
    mcp_reg = _mcp_registry(request)
    if mcp_reg is not None:
        for mcp in mcp_reg.list_mcps():
            mcp_name = getattr(mcp, "name", "") or ""
            mcps.append(
                {
                    "name": mcp_name,
                    "description": (
                        getattr(mcp, "description", None)
                        or getattr(
                            getattr(mcp, "mcp_config", None),
                            "url",
                            "",
                        )
                        or ""
                    ),
                },
            )

    return {"mcps": mcps}


# ------------------------------------------------------------------
# GET /search  — search tools or mcps by name (global)
# ------------------------------------------------------------------


@catalog_router.get(
    "/search",
    summary="Search tools or MCPs by name",
)
async def search_tools_or_mcps(
    type: str,
    name: str = "",
    request: Request = Request,
) -> dict:
    """Search tools or MCP servers by name.

    Args:
        type: ``tools`` or ``mcps``.
        name: Keyword to filter by name (case-insensitive, substring match).
              Empty string returns all.

    Response::

        {"tools": [...]}   // when type=tools
        {"mcps": [...]}    // when type=mcps
    """
    keyword = (name or "").lower()

    if type == "tools":
        items: list[dict] = []
        for tool in _tool_registry(request).list_tools():
            n = _tool_name(tool)
            if n in _HIDDEN_PROJECT_TOOLS:
                continue
            if keyword and keyword not in n.lower():
                continue
            items.append(
                {
                    "name": n,
                    "description": getattr(tool, "description", "") or "",
                },
            )
        for meta in _enterprise_tools_meta():
            n = meta["name"]
            if keyword and keyword not in n.lower():
                continue
            items.append(
                {
                    "name": n,
                    "description": meta.get("description", ""),
                },
            )
        return {"tools": items}

    if type == "mcps":
        items: list[dict] = []
        mcp_reg = _mcp_registry(request)
        if mcp_reg is not None:
            for mcp in mcp_reg.list_mcps():
                mcp_name = getattr(mcp, "name", "") or ""
                if keyword and keyword not in mcp_name.lower():
                    continue
                items.append(
                    {
                        "name": mcp_name,
                        "description": (
                            getattr(mcp, "description", None)
                            or getattr(
                                getattr(mcp, "mcp_config", None),
                                "url",
                                "",
                            )
                            or ""
                        ),
                    },
                )
        return {"mcps": items}

    raise HTTPException(
        status_code=400,
        detail="Invalid type: must be 'tools' or 'mcps'",
    )


# ------------------------------------------------------------------
# internal
# ------------------------------------------------------------------


def _tool_name(tool: object) -> str:
    """Best-effort tool name extraction (mirrors ToolRegistry._tool_name)."""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    fn = getattr(tool, "func", None) or getattr(tool, "_func", None)
    if callable(fn):
        return getattr(fn, "__name__", "") or ""
    return getattr(tool, "__name__", "") or ""


__all__ = [
    "agent_tools_router",
    "catalog_router",
    "_tool_whitelists",
    "_get_enabled_tools",
    "_set_enabled_tools",
    "_configurable_tool_names",
    "load_tool_whitelists",
    "_FRAMEWORK_TOOLS_META",
]
