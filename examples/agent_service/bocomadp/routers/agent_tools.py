# -*- coding: utf-8 -*-
"""Agent tool management — query & toggle per-agent tool enablement.

Endpoints
---------
``GET    /agents/{agent_id}/tools``           — list tools with status
``PUT    /agents/{agent_id}/tools/{name}``    — enable a tool
``DELETE /agents/{agent_id}/tools/{name}``    — disable a tool

Tool sources — the configurable set ``M``, single-sourced from
:mod:`bocomadp.tool_catalog`:

1. **Workspace builtins** — ``Bash/Read/Write/Edit/Glob/Grep``
   (runtime names, capitalized; see ``agentscope.tool._builtin``).
2. **Project tools** — from ``ToolRegistry`` (builtin_tools.py + custom/).
3. **MCP servers** — names from ``McpRegistry``.
4. **Framework team/planning tools** — ``Team*`` / ``Task*``.
5. **Enterprise tools** — built by ``build_enterprise_tools``
   (online search and the placeholder tools are excluded).

Semantics
---------
``enabled_tools == []`` means **every tool in M is enabled**.
The first *disable* operation expands ``[]`` to the full M list minus
the disabled tool.  Subsequent toggles are plain list add / remove.

Every tool in M is toggleable — including the workspace builtins.
The built-in agent-creator additionally *displays* its own factory
tools on ``GET``; those are display-only (``toggleable=False``) and
are intentionally not configurable.
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

# ------------------------------------------------------------------
# workspace builtins — 名称/描述统一取自 bocomadp.tool_catalog
# ------------------------------------------------------------------
#: 注意名字是**运行时真值**（首字母大写）。历史上这里用的是小写名，
#: 与运行时 ``Bash`` 等对不上，导致白名单对 builtins 失效。
_BUILTIN_TOOLS: list[dict] = [dict(m) for m in BUILTIN_TOOLS_META]

#: 团队/规划工具的静态元数据（name + 简短 description）。
#: 单一数据源：``_all_tool_names()`` 据此推导可管理的工具名集合，
#: ``GET /agents/{id}/tools`` 据此输出带 description 的展示条目。
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
    """Return the current enabled-tools list for *agent_id*."""
    return list(_tool_whitelists.get(agent_id, []))


def _set_enabled_tools(agent_id: str, tools: list[str]) -> None:
    """Persist *tools* for *agent_id*."""
    _tool_whitelists[agent_id] = tools
    _persist_whitelists()


def _resolve_enabled(all_tool_names: list[str], whitelist: list[str]) -> set[str]:
    """Return the *set* of enabled tool names.

    When *whitelist* is empty every tool is enabled; otherwise only
    names in *whitelist* are active.
    """
    if not whitelist:
        return set(all_tool_names)
    return {n for n in whitelist if n in all_tool_names}


def _all_tool_names(request: Request) -> set[str]:
    """Every known tool name across all sources (the configurable set M)."""
    names: set[str] = {bt["name"] for bt in _BUILTIN_TOOLS}
    names.update(_tool_registry(request).list_tool_names())
    mcp_reg = _mcp_registry(request)
    if mcp_reg is not None:
        for mcp in mcp_reg.list_mcps():
            name = getattr(mcp, "name", "") or ""
            if name:
                names.add(name)
    # 团队/规划工具由框架 get_toolkit 挂载，纳入白名单接口管理。
    names.update(m["name"] for m in _FRAMEWORK_TOOLS_META)
    # 企业工具由 build_enterprise_tools 主动构建，纳入白名单接口管理。
    # 名字取自工具实例，自动跟随 BOCOMADP_TOOL_ASCII_NAMES 切换中/英文。
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
    """Return every tool in M, annotated with its enabled state.

    All configurable tools (builtins + project + framework + enterprise)
    are returned in a flat ``tools`` list; MCP servers are in a separate
    ``mcps`` list.  For the built-in agent-creator, its own factory tools
    are appended as display-only entries (``toggleable=False``).

    Response::

        {
          "agent_id": "...",
          "tools": [
            {"name": "Bash", "description": "...", "enabled": true, "toggleable": true},
            {"name": "echo", "description": "...", "enabled": false, "toggleable": true}
          ],
          "mcps": [
            {"name": "browser-use", "description": "...", "enabled": true, "toggleable": true}
          ]
        }
    """
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    all_names = sorted(_all_tool_names(request))
    enabled_tools = _get_enabled_tools(agent_id)
    enabled_names = _resolve_enabled(all_names, enabled_tools)

    tools: list[dict] = []
    mcps: list[dict] = []

    # 1. Workspace builtins + project tools → merged into `tools`
    for bt in _BUILTIN_TOOLS:
        tools.append({**bt, "enabled": bt["name"] in enabled_names, "toggleable": True})

    for tool in _tool_registry(request).list_tools():
        name = _tool_name(tool)
        tools.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
                "enabled": name in enabled_names,
                "toggleable": True,
            },
        )

    # 1b. 团队/规划工具（框架 get_toolkit 挂载）→ 追加进 `tools`，带简短 description
    for meta in _FRAMEWORK_TOOLS_META:
        name = meta["name"]
        tools.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "enabled": name in enabled_names,
                "toggleable": True,
            },
        )

    # 1c. 企业工具（build_enterprise_tools 主动构建）→ 纳入可配置集合
    for meta in _enterprise_tools_meta():
        name = meta["name"]
        tools.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "enabled": name in enabled_names,
                "toggleable": True,
            },
        )

    # 1d. 智能体工厂自带的工厂工具 → 仅展示，不可配置
    if agent_id == AGENT_CREATOR_ID:
        for meta in _factory_tools_meta():
            tools.append(
                {
                    "name": meta["name"],
                    "description": meta.get("description", ""),
                    "enabled": True,
                    "toggleable": False,
                },
            )

    # 2. MCP servers → separate `mcps` list
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
                    "enabled": mcp_name in enabled_names,
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


@agent_tools_router.put(
    "/{agent_id}/tools/{tool_name}",
    summary="Enable a tool for the agent",
)
async def enable_agent_tool(
    agent_id: str,
    tool_name: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Add *tool_name* to the agent's enabled-tools whitelist."""
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    toggleable = _all_tool_names(request)
    if tool_name not in toggleable:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    current = _get_enabled_tools(agent_id)

    # [] means all enabled → already enabled, nothing to do
    if not current:
        _set_enabled_tools(agent_id, current)  # keep []
        logger.info("agent_tools: %s enable %s (already all-enabled)", agent_id, tool_name)
        return {"ok": True}

    if tool_name in current:
        logger.info("agent_tools: %s enable %s (already enabled)", agent_id, tool_name)
        return {"ok": True}

    current.append(tool_name)
    _set_enabled_tools(agent_id, current)
    logger.info("agent_tools: %s enable %s → enabled_tools=%s", agent_id, tool_name, current)
    return {"ok": True}


# ------------------------------------------------------------------
# DELETE /agents/{agent_id}/tools/{tool_name}   — disable
# ------------------------------------------------------------------


@agent_tools_router.delete(
    "/{agent_id}/tools/{tool_name}",
    summary="Disable a tool for the agent",
)
async def disable_agent_tool(
    agent_id: str,
    tool_name: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Remove *tool_name* from the agent's enabled-tools whitelist.

    When ``enabled_tools`` is empty (all-enabled), it is first expanded
    to the full tool list so the disable can take effect.
    """
    agent = await _resolve_framework_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    toggleable = _all_tool_names(request)
    if tool_name not in toggleable:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    current = _get_enabled_tools(agent_id)

    # [] → expand to full list first, then remove
    if not current:
        current = list(toggleable)

    if tool_name not in current:
        logger.info("agent_tools: %s disable %s (already disabled)", agent_id, tool_name)
        return {"ok": True}

    current.remove(tool_name)
    _set_enabled_tools(agent_id, current)
    logger.info("agent_tools: %s disable %s → enabled_tools=%s", agent_id, tool_name, current)
    return {"ok": True}


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
    "_tool_whitelists",
    "_get_enabled_tools",
    "_set_enabled_tools",
    "load_tool_whitelists",
    "_FRAMEWORK_TOOLS_META",
]
