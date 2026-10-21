# -*- coding: utf-8 -*-
"""Expert-team tooling injected at the ``get_toolkit`` boundary.

The framework's :func:`agentscope.app._service._toolkit.get_toolkit`
attaches a generic team toolset: a ``TeamSay`` tool per session role, and
an ``AgentInvite`` whenever the top-level invitable pool is non-empty.

The expert-team product adds two behaviours on top of that generic
toolset, both of which used to live in the framework function:

1. **Strict-workflow handoff.** When the leader's ``team_config`` runs in
   ``collaboration_mode="workflow"``, ``TeamSay`` must hard-enforce the
   configured edges:

   - a *worker* may only report back to the leader (the hub of the
     sequential chain);
   - the *leader* may only ``TeamSay`` to the ``to`` endpoints of its
     configured ``handoff_relations``.

   The enforcement lives in :mod:`._team_tool_patch`'s class-level
   ``TeamSay.__call__`` replacement (it checks
   ``self._allowed_handoff_targets`` when non-None); this module computes
   the allowed set from the team config and writes it onto the tool
   instance.

2. **Invitable-pool backfill.** ``list_resource`` hides team members
   (``parent_agent_id`` set), so the framework's top-level pool can be
   empty for a leader whose members are all within the team. Storage-level
   agents are merged back in so ``AgentInvite`` still attaches (it can
   borrow the team's configured members at runtime).

Everything is driven by the session's team role, mirroring the framework
rules: a worker session sees only ``TeamSay``; every other session gets
the leader-side toolset plus (if a pool exists) ``AgentInvite``.

The patch is idempotent and bound to the ``_chat`` module global, the
only call site — so there is no import-order race.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.team_toolkit")

_original_get_toolkit: Any = None


async def _allowed_handoff_targets(
    storage: Any,
    user_id: str,
    agent_record: Any,
    session_record: Any,
) -> tuple[Any, str | None, set[str] | None]:
    """Resolve (team, team_role, allowed_handoff_targets).

    ``team`` is ``None`` for non-team sessions; ``team_role`` is
    ``"leader"``/``"worker"`` inside a team and ``None`` outside it (a
    non-team session still gets leader-side tools from the framework).
    """
    team: Any = None
    team_role: str | None = None
    if session_record.team_id is not None:
        team = await storage.get_team(user_id, session_record.team_id)
        if team is not None:
            team_role = (
                "leader" if team.session_id == session_record.id else "worker"
            )

    from bocomadp import team_store

    allowed: set[str] | None = None
    if team_role == "worker" and team is not None:
        # Strict-workflow: a worker may only report back to the leader.
        leader_session = await storage.get_session(
            user_id,
            "",
            team.session_id,
        )
        if leader_session is not None:
            rel = await team_store.get_team(
                storage,
                user_id,
                leader_session.agent_id,
            )
            if rel is not None and rel.collaboration_mode == "workflow":
                allowed = {leader_session.agent_id}
    else:
        # Leader-side: allow TeamSay to every ``to`` endpoint of the
        # configured workflow edges (hub-and-spoke delegation).
        rel = await team_store.get_team(storage, user_id, agent_record.id)
        if (
            rel is not None
            and rel.collaboration_mode == "workflow"
            and rel.handoff_relations
        ):
            allowed = {r.to_agent_id for r in rel.handoff_relations}

    return team, team_role, allowed


_ROUTING_TOOL_NAMES = {
    "TeamCreate",
    "AgentCreate",
    "AgentInvite",
    "TeamSay",
    "TeamDelete",
}


async def _apply_orchestrated_toolkit(
    toolkit: Any,
    *,
    storage: Any,
    user_id: str,
    agent_record: Any,
    session_record: Any,
    team: Any,
    team_role: str | None,
) -> bool:
    """Replace the generic team toolset with workflow control tools.

    Returns ``True`` when the session belongs to an orchestrated team and
    the caller should skip the legacy TeamSay/AgentInvite handling.
    """
    if team is None:
        return False
    leader_session = await storage.get_session(user_id, "", team.session_id)
    if leader_session is None:
        return False
    from bocomadp import team_store

    rel = await team_store.get_team(storage, user_id, leader_session.agent_id)
    if rel is None or rel.collaboration_mode != "workflow":
        return False

    from bocomadp.team.state import get_state
    from bocomadp.team.tools import ResumeWorkflow, SubmitHandoffs

    state = await get_state(storage, team.id)
    if state is None:
        return False

    groups = getattr(toolkit, "tool_groups", None) or []
    for group in groups:
        group.tools = [
            tool
            for tool in getattr(group, "tools", None) or []
            if getattr(tool, "name", "") not in _ROUTING_TOOL_NAMES
        ]

    common = {
        "storage": storage,
        "user_id": user_id,
        "session_id": session_record.id,
        "agent_id": agent_record.id,
        "team_id": team.id,
    }
    if team_role == "leader":
        if state.leader.phase == "first" and not state.leader.dispatched:
            await toolkit.add_tool(
                SubmitHandoffs(**common, is_leader=True),
                group_name="basic",
            )
        if state.leader.phase == "first" and state.status in ("running", "failed"):
            await toolkit.add_tool(
                ResumeWorkflow(**common),
                group_name="basic",
            )
    else:
        node = state.nodes.get(agent_record.id)
        if node is not None and node.status in (
            "active",
            "interrupted",
            "pending",
            "failed",
        ):
            await toolkit.add_tool(
                SubmitHandoffs(**common, is_leader=False),
                group_name="basic",
            )
    return True


async def _get_toolkit_with_team(*args: Any, **kwargs: Any):
    """Assemble the toolkit, then inject the expert-team behaviours."""
    toolkit = await _original_get_toolkit(*args, **kwargs)
    if toolkit is None:
        return toolkit

    storage = kwargs.get("storage")
    user_id = kwargs.get("user_id")
    agent_record = kwargs.get("agent_record")
    session_record = kwargs.get("session_record")
    if (
        storage is None
        or user_id is None
        or agent_record is None
        or session_record is None
    ):
        return toolkit

    from agentscope.app._tool import AgentInvite, TeamSay

    team, team_role, allowed = await _allowed_handoff_targets(
        storage,
        user_id,
        agent_record,
        session_record,
    )

    if await _apply_orchestrated_toolkit(
        toolkit,
        storage=storage,
        user_id=user_id,
        agent_record=agent_record,
        session_record=session_record,
        team=team,
        team_role=team_role,
    ):
        return toolkit

    groups = getattr(toolkit, "tool_groups", None) or []
    has_invite = False
    for group in groups:
        for tool in getattr(group, "tools", None) or []:
            if isinstance(tool, TeamSay):
                tool._allowed_handoff_targets = allowed
            elif isinstance(tool, AgentInvite):
                has_invite = True

    # Leader-side backfill: if the framework attached no AgentInvite
    # (top-level invitable pool empty because team members are hidden by
    # list_resource), merge storage-level agents back in and attach it.
    if team_role != "worker" and not has_invite:
        from agentscope.app.access import ResourceKind

        resource_access_service = kwargs.get("resource_access_service")
        visible = await resource_access_service.list_resource(
            user_id,
            ResourceKind.AGENT,
        )
        pool_by_id: dict[str, Any] = {view.id: view for view in visible}
        for record in await storage.list_agents(user_id):
            pool_by_id.setdefault(record.id, record)
        invitable_pool = [
            agent
            for agent in pool_by_id.values()
            if agent.data.invite_config.invitable
            and (agent.data.invite_config.invite_description or "").strip()
        ]
        if invitable_pool:
            invite = AgentInvite(
                storage=storage,
                message_bus=kwargs.get("message_bus"),
                workspace_manager=kwargs.get("workspace_manager"),
                user_id=user_id,
                session_id=session_record.id,
                agent_id=agent_record.id,
                invitable_pool=invitable_pool,
            )
            await toolkit.add_tool(invite, group_name="basic")
            logger.debug(
                "backfilled AgentInvite for leader session %s "
                "(pool of %d invitable agents)",
                session_record.id,
                len(invitable_pool),
            )

    return toolkit


_TEAM_CREATE_CLEANUP_HINT = (
    "\n\n## 任务完成后的团队清理\n"
    "当所有成员已完成任务、你已收齐结果并准备给出最终答复后，"
    "建议调用 `TeamDelete` 解散团队。\n"
    "- 会删除：运行时团队记录、团队成员会话；由 AgentCreate 自建的"
    "成员 agent 也会被删除；AgentInvite 邀请的成员 agent 保留；\n"
    "- 会保留：leader 会话、共享工作区/PVC、已写入的文件；\n"
    "- 如果成员仍在执行，或你还需要继续派活、追问成员，不要删除；\n"
    "- 删除不可逆，删除后团队工具不可用；需要继续协作时必须重新建团。\n"
)


def install_team_create_cleanup_hint() -> None:
    """Append the post-task cleanup guidance to TeamCreate's tool prompt.

    The hint lives on the tool description rather than a specific leader
    briefing so it reaches plain agents that create a team dynamically as
    well as configured free_handoff leaders. Workflow mode removes
    ``TeamCreate`` entirely, so orchestrated sessions are unaffected.
    """
    from agentscope.app._tool import TeamCreate

    marker = "## 任务完成后的团队清理"
    if marker in TeamCreate.description:
        return
    TeamCreate.description = (
        TeamCreate.description.rstrip() + _TEAM_CREATE_CLEANUP_HINT
    )
    logger.info("installed TeamCreate post-task cleanup hint")


def patch_team_toolkit() -> None:
    """Replace the chat service's ``get_toolkit`` binding (idempotent).

    Must run before ``patch_get_toolkit`` (the whitelist wrapper) so the
    whitelist sees the team-injected toolkit; either alone is fine too.
    """
    global _original_get_toolkit
    if _original_get_toolkit is not None:
        return

    install_team_create_cleanup_hint()

    from bocomadp._team_tool_patch import install_team_tool_patches

    install_team_tool_patches()

    from agentscope.app._service import _chat as _chat_module

    _original_get_toolkit = _chat_module.get_toolkit
    _chat_module.get_toolkit = _get_toolkit_with_team
    logger.info(
        "patched %s.get_toolkit with expert-team tooling",
        _chat_module.__name__,
    )


__all__ = ["patch_team_toolkit"]
