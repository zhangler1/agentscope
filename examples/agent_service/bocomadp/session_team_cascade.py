# -*- coding: utf-8 -*-
"""Expert-team cascade on ``SessionService.delete_agent``.

A leader agent may carry a persistent expert-team config
(``AgentData.team_config`` + members pointing back via
``parent_agent_id``). When the leader is deleted we must:

- self-built members (``parent_agent_id == agent_id``) -> cascade-delete
  them (they exist only under this team);
- invited/referenced members (present in some leader's ``member_ids`` but
  not owned by this leader) -> only detach them from that leader's
  ``member_ids`` / ``handoff_relations`` (the underlying agent record is
  preserved, matching the permission-isolation rule).

This is expert-team policy, so instead of living inside the framework's
:class:`SessionService` we wrap ``delete_agent`` from the plugin layer.
The wrapper runs the cascade *before* the original delete so child
deletes complete first, then delegates to the framework implementation.

The patch is class-level and idempotent, so any instance handed out by
``get_session_service`` picks it up.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.session_team_cascade")

_original_delete_agent: Any = None


async def _delete_agent_with_cascade(
    self: Any,
    user_id: str,
    agent_id: str,
) -> bool:
    """Run the expert-team cascade, then the framework delete.

    Mirrors the removed framework block: storage.list_agents() hides team
    members (parent_agent_id set), so self-built children are enumerated
    through the leader's own team_config.member_ids, resolved directly via
    get_agent.
    """
    from bocomadp import team_store

    # Leader case: cascade-delete self-built members, then dissolve the
    # team row itself.
    leader_team = await team_store.get_team(self._storage, user_id, agent_id)
    if leader_team is not None:
        for mid in list(leader_team.member_ids):
            if leader_team.is_self_built(mid):
                await self.delete_agent(user_id, mid)
        await team_store.delete_team(self._storage, user_id, agent_id)

    # Detach this agent from any leader that references it (invited
    # member case). Team rosters live in ``expert_team_relations`` now.
    for other_team in await team_store.list_teams(self._storage, user_id):
        if other_team.leader_agent_id == agent_id:
            continue
        if other_team.remove_member(agent_id):
            other_team.handoff_relations = [
                r
                for r in other_team.handoff_relations
                if r.from_agent_id != agent_id
                and r.to_agent_id != agent_id
            ]
            await team_store.upsert_team(self._storage, other_team)

    return await _original_delete_agent(self, user_id, agent_id)


def patch_session_team_cascade() -> None:
    """Wrap ``SessionService.delete_agent`` (idempotent).

    Must run before the first agent deletion; the wrapper is bound at the
    class level, so it applies to every service instance.
    """
    global _original_delete_agent
    if _original_delete_agent is not None:
        return

    from agentscope.app._service import _session as _session_module

    _original_delete_agent = _session_module.SessionService.delete_agent
    _session_module.SessionService.delete_agent = _delete_agent_with_cascade
    logger.info(
        "patched %s.delete_agent with expert-team cascade",
        _session_module.SessionService.__name__,
    )


__all__ = ["patch_session_team_cascade"]
