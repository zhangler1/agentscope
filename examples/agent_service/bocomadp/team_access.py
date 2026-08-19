# -*- coding: utf-8 -*-
"""Expert-team member-list filter + ``is_self_built`` flag, applied at the
:class:`ResourceAccessService.list_resource` boundary.

The framework's ``list_resource`` deliberately hides team members
(``parent_agent_id`` set) so the top-level agent list stays clean — that is
the correct behaviour for the framework, and it must not be changed there.
The expert-team router, however, needs to fetch a team's member list on
demand by passing ``parent_agent_id``.

Without touching framework code we wrap ``list_resource`` here:

- when ``parent_agent_id`` is provided (AGENT kind only), we read the
  leader's ``team_config.member_ids`` so invited-by-reference members
  (present in ``member_ids`` but without a ``parent_agent_id`` backlink)
  also surface, and each returned view gets its ``is_self_built`` flag set
  (``True`` for backlinked members, ``False`` for invited-by-reference).
- top-level listings pass straight through to the original implementation.

The patch is class-level and idempotent, so any instance handed out by
``get_resource_access_service`` picks it up.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.team_access")

_original_list_resource: Any = None


async def _team_list_resource(
    self: Any,
    viewer_id: str,
    kind: Any,
    parent_agent_id: str | None = None,
) -> list[Any]:
    """List resources, adding the team-member filter when requested.

    Only AGENT listings with a non-None ``parent_agent_id`` take the
    expert-team path; every other call is delegated unchanged.
    """
    from agentscope.app.access import ResourceKind

    if kind is not ResourceKind.AGENT:
        return await _original_list_resource(self, viewer_id, kind)

    from bocomadp import team_store
    from bocomadp.routers._schema.agent import TeamAgentView

    if parent_agent_id is None:
        # Top-level listing: hide self-built members (they exist only
        # under their team), keep leaders / plain / invited-by-reference
        # agents, and re-derive the ``is_team`` flag from the team table.
        teams = await team_store.list_teams(self._storage, viewer_id)
        hidden = {
            mid
            for team in teams
            for mid in team.member_ids
            if team.is_self_built(mid)
        }
        leaders = {team.leader_agent_id for team in teams}
        enriched: list[Any] = []
        for view in await _original_list_resource(self, viewer_id, kind):
            if view.id in hidden:
                continue
            enriched.append(
                TeamAgentView.model_validate(
                    {
                        **view.model_dump(),
                        "is_team": view.id in leaders,
                    },
                ),
            )
        return enriched

    # Team-member listing for a specific leader. The roster lives in the
    # ``expert_team_relations`` table; ``relation`` tells self-built from
    # invited-by-reference members.
    rel = await team_store.get_team(
        self._storage,
        viewer_id,
        parent_agent_id,
    )
    member_ids: set[str] = set(rel.member_ids) if rel is not None else set()

    def _is_member(record: Any) -> bool:
        return record.source != "team" and record.id in member_ids

    views: list[Any] = []
    seen: set[tuple[str, str]] = set()

    def _to_member_view(record: Any, editable: bool) -> TeamAgentView:
        is_self_built = (
            rel.is_self_built(record.id) if rel is not None else False
        )
        base = self._build_view(record, viewer_id, editable)
        return TeamAgentView.model_validate(
            {
                **base.model_dump(),
                "is_team": False,
                "is_self_built": is_self_built,
                "parent_agent_id": (
                    parent_agent_id if is_self_built else None
                ),
            },
        )

    for record in await self._storage.list_agents(viewer_id):
        if not _is_member(record):
            continue
        views.append(_to_member_view(record, True))
        seen.add((record.user_id, record.id))

    # Cross-owner shared members (policy refs) stay visible too.
    from agentscope.app.access import ResourcePermission

    for ref in await self._list_refs(viewer_id, kind):
        key = (ref.owner_id, ref.resource_id)
        if key in seen:
            continue
        record = await self._get_owned(kind, ref.owner_id, ref.resource_id)
        if record is None or not _is_member(record):
            continue
        views.append(
            _to_member_view(
                record,
                ref.permission == ResourcePermission.EDIT,
            ),
        )
        seen.add(key)

    return views


def patch_team_access() -> None:
    """Wrap ``ResourceAccessService.list_resource`` (idempotent).

    Must run before the first expert-team list call; the wrapper is bound
    at the class level, so it applies to every service instance.
    """
    global _original_list_resource
    if _original_list_resource is not None:
        return

    from agentscope.app._service import _access as _access_module

    _original_list_resource = _access_module.ResourceAccessService.list_resource
    _access_module.ResourceAccessService.list_resource = _team_list_resource
    logger.info(
        "patched %s.list_resource with expert-team member filter",
        _access_module.ResourceAccessService.__name__,
    )


__all__ = ["patch_team_access"]
