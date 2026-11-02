# -*- coding: utf-8 -*-
"""Workspace binding for team workers.

Every worker session in a runtime team uses the leader's current session
workspace: same PVC, same workdir, same regular directory layout. The wrapper
does not care about ``collaboration_mode``; as soon as a session belongs to a
team and is not the leader session, the leader workspace is used. All other
sessions (ordinary agents, sessions without a team) delegate unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.team.workspace")


class TeamWorkspaceManager:
    """Wrap a workspace manager and bind team workers to the leader workspace.

    The session's ``config.workspace_id`` remains a lookup input; for a team
    worker it is replaced by the leader session's workspace id. This keeps the
    binding correct even for free_handoff workers whose ``AgentInvite`` session
    was created with the invited agent's own workspace id.
    """

    def __init__(self, inner: Any, storage: Any) -> None:
        self._inner = inner
        self._storage = storage

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
    ) -> Any:
        try:
            session = await self._storage.get_session(
                user_id,
                agent_id,
                session_id,
            )
            if session is not None and session.team_id:
                team = await self._storage.get_team(user_id, session.team_id)
                if team is not None and team.session_id != session_id:
                    leader_session = await self._storage.get_session(
                        user_id,
                        "",
                        team.session_id,
                    )
                    if leader_session is not None:
                        return await self._inner.get_workspace(
                            user_id,
                            leader_session.agent_id,
                            team.session_id,
                            leader_session.config.workspace_id,
                        )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "team workspace binding failed for session %s; "
                "falling back to the session's own workspace",
                session_id,
            )
        return await self._inner.get_workspace(
            user_id,
            agent_id,
            session_id,
            workspace_id,
        )

    async def __aenter__(self) -> "TeamWorkspaceManager":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> Any:
        return await self._inner.__aexit__(*exc)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


# Backward-compatible alias for callers that imported the old name.
WorkflowWorkspaceManager = TeamWorkspaceManager

__all__ = ["TeamWorkspaceManager", "WorkflowWorkspaceManager"]
