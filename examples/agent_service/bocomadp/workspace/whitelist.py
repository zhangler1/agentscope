# -*- coding: utf-8 -*-
"""Per-agent tool whitelist applied to workspace MCPs.

The framework injects MCPs straight from ``workspace.list_mcps()``
(see ``agentscope.app._service._toolkit.get_toolkit``), bypassing
``extra_agent_tools`` entirely — so the whitelist maintained by
``agent_tools_router`` (PUT/DELETE ``/agents/{id}/tools/{name}``)
cannot filter MCPs at the ``extra_factory`` layer the way it filters
project tools.

Fix without touching framework code: wrap the workspace manager.
``WorkspaceManagerBase.get_workspace`` already receives ``agent_id``,
so the wrapper intercepts it and returns a delegating proxy whose
``list_mcps`` applies the per-agent whitelist.  ``get_toolkit`` calls
``list_mcps`` on every chat run, so whitelist changes take effect
immediately.
"""

from __future__ import annotations

from typing import Any

from agentscope.workspace import WorkspaceBase


class _WhitelistWorkspaceProxy(WorkspaceBase):
    """Delegating workspace proxy filtering ``list_mcps`` per agent.

    必须继承 :class:`WorkspaceBase`：框架 ``Agent._get_system_prompt``
    用 ``isinstance(self.offloader, WorkspaceBase)`` 判断是否把工作区
    instructions（workspace 提示词）追加进系统提示词——普通委托类不满足
    该检查，会导致 workspace 提示词被静默丢弃。抽象方法全部委托给
    真实工作区，行为不变。
    """

    def __init__(self, workspace: Any, agent_id: str, mcp_registry: Any = None) -> None:
        object.__setattr__(self, "_workspace", workspace)
        object.__setattr__(self, "_agent_id", agent_id)
        object.__setattr__(self, "_mcp_registry", mcp_registry)

    # ── WorkspaceBase 抽象方法：全部委托给真实工作区 ────────────

    async def initialize(self) -> None:
        return await self._workspace.initialize()

    async def close(self) -> None:
        return await self._workspace.close()

    def get_backend(self) -> Any:
        return self._workspace.get_backend()

    async def get_instructions(self) -> str:
        return await self._workspace.get_instructions()

    async def add_mcp(self, mcp_client: Any) -> None:
        return await self._workspace.add_mcp(mcp_client)

    async def remove_mcp(self, name: str) -> None:
        return await self._workspace.remove_mcp(name)

    # ── WorkspaceBase 已实现方法：显式转发，保持与真实工作区一致 ──
    # 继承后这些方法默认走基类实现，会绕开 LocalWorkspace 等子类
    # 的覆写（如 hash 索引的 list_skills、PowerShell 的 list_tools），
    # 因此逐一转发到被代理的工作区。

    async def reset(self) -> None:
        return await self._workspace.reset()

    async def list_tools(self) -> list:
        return await self._workspace.list_tools()

    async def list_skills(self) -> list:
        return await self._workspace.list_skills()

    async def add_skill(self, skill_path: str) -> None:
        return await self._workspace.add_skill(skill_path)

    async def add_skill_archive(self, *args: Any, **kwargs: Any) -> None:
        return await self._workspace.add_skill_archive(*args, **kwargs)

    async def remove_skill(self, name: str) -> None:
        return await self._workspace.remove_skill(name)

    async def offload_context(self, *args: Any, **kwargs: Any) -> Any:
        return await self._workspace.offload_context(*args, **kwargs)

    async def offload_tool_result(self, *args: Any, **kwargs: Any) -> Any:
        return await self._workspace.offload_tool_result(*args, **kwargs)

    # ── 代理特有：按智能体白名单过滤 MCP ────────────────────────

    async def list_mcps(self) -> list:
        """Return MCPs allowed by the per-agent tool whitelist.

        Whitelist empty + usableTools empty → no MCPs available
        (enterprise tools / MCPs require explicit enablement).

        If a whitelisted MCP is missing from the workspace (e.g. it was
        not part of ``default_mcps`` at startup), auto-register it from
        ``mcp_registry`` so that ``PUT /agents/{id}/tools/{name}`` takes
        effect without restarting the workspace.
        """
        from bocomadp.routers.agent_tools import _tool_whitelists
        from bocomadp.tools.enterprise import usable_enterprise_tool_names
        from bocomadp.deerflow.custom_params import get_custom_params

        mcps = await self._workspace.list_mcps()
        whitelist = _tool_whitelists.get(self._agent_id, [])
        usable = get_custom_params().get("usableTools")
        allowed = set(whitelist) | usable_enterprise_tool_names(usable)

        existing_names = {getattr(m, "name", "") for m in mcps}
        missing_names = allowed - existing_names

        if missing_names and self._mcp_registry is not None:
            registry_map = {
                getattr(m, "name", ""): m
                for m in self._mcp_registry.list_mcps()
            }
            for name in missing_names:
                spec = registry_map.get(name)
                if spec is None:
                    continue
                try:
                    await self._workspace.add_mcp(spec)
                except (ValueError, RuntimeError):
                    pass
            mcps = await self._workspace.list_mcps()

        return [m for m in mcps if getattr(m, "name", "") in allowed]

    def __getattr__(self, item: str) -> Any:
        # Everything else (list_tools / list_skills / get_backend /
        # add_mcp / add_skill_archive / workdir / ...) delegates.
        return getattr(self._workspace, item)


class WhitelistWorkspaceManager:
    """Wrap a ``WorkspaceManagerBase``, filtering MCPs per agent.

    All methods and attributes except ``get_workspace`` and the
    lifecycle hooks delegate to the inner manager, so local and K8s
    managers both work unchanged.
    """

    def __init__(self, inner: Any, mcp_registry: Any = None) -> None:
        self._inner = inner
        self._mcp_registry = mcp_registry

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> Any:
        ws = await self._inner.get_workspace(
            user_id,
            agent_id,
            session_id,
            workspace_id,
        )
        return _WhitelistWorkspaceProxy(ws, agent_id, self._mcp_registry)

    async def __aenter__(self) -> "WhitelistWorkspaceManager":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> Any:
        return await self._inner.__aexit__(*exc)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


__all__ = ["WhitelistWorkspaceManager"]
