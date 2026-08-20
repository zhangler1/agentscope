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

    _, team_role, allowed = await _allowed_handoff_targets(
        storage,
        user_id,
        agent_record,
        session_record,
    )

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


def patch_team_toolkit() -> None:
    """Replace the chat service's ``get_toolkit`` binding (idempotent).

    Must run before ``patch_get_toolkit`` (the whitelist wrapper) so the
    whitelist sees the team-injected toolkit; either alone is fine too.
    """
    global _original_get_toolkit
    if _original_get_toolkit is not None:
        return

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
