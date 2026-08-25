# -*- coding: utf-8 -*-
"""Query cumulative token usage for a session.

Endpoint
--------
``GET /sessions/{session_id}/usage?agent_id=xxx&user_id=xxx``

    Returns ``input_tokens``, ``output_tokens`` and ``message_count``
    aggregated across all persisted messages in the session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from agentscope.app.deps import get_current_user_id

logger = logging.getLogger("bocomadp.session_usage")

session_usage_router = APIRouter(
    prefix="/sessions",
    tags=["session-usage"],
)


@session_usage_router.get(
    "/{session_id}/usage",
    summary="Get cumulative token usage for a session",
)
async def get_session_usage(
    session_id: str,
    agent_id: str = Query(default="default", description="Agent id"),
    user_id: str = Query(default="default", description="User id"),
    request: Request = None,  # type: ignore[assignment]
) -> dict:
    """Sum token usage across all messages in a session.

    Iterates through the session's message list via paginated
    ``list_messages``, accumulating ``usage.input_tokens`` and
    ``usage.output_tokens`` from every :class:`Msg` that has them.
    """
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage backend not available",
        )

    # Check session ownership
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    total_input = 0
    total_output = 0
    message_count = 0
    before: str | None = None
    batch_limit = 200

    while True:
        messages, has_more = await storage.list_messages(
            user_id,
            session_id,
            limit=batch_limit,
            before=before,
        )
        for msg in messages:
            message_count += 1
            u = getattr(msg, "usage", None)
            if u is not None:
                total_input += getattr(u, "input_tokens", 0) or 0
                total_output += getattr(u, "output_tokens", 0) or 0

        if not has_more or not messages:
            break
        # Move cursor to continue pagination
        before = messages[0].id

    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "message_count": message_count,
    }


@session_usage_router.get(
    "/limit",
    summary="Paginated session ids for an agent (direct DB query)",
)
async def list_session_ids_paginated(
    agent_id: str = Query(default="default", description="Agent id"),
    user_id: str = Depends(get_current_user_id),
    page: int = Query(default=1, ge=1, description="Page number, starts at 1"),
    page_size: int = Query(
        default=20,
        ge=1,
        le=200,
        description="Number of items per page (1-200)",
    ),
) -> dict:
    """Return a paginated list of session records for an agent.

    The payload shape mirrors the framework's ``GET /sessions/``
    (``ListSessionsResponse``): a ``sessions`` array of full
    :class:`SessionRecord` objects, plus ``total``. On top of that we
    also expose ``agent_id``, ``page``, ``page_size`` and ``has_more``
    for the client-side pagination.

    Queries the ``sessions`` table directly via the shared async engine
    (same DB URL as the framework storage, reuse ``pool_config``'s
    lazy-loaded engine to avoid opening an extra connection pool), so
    pagination is pushed down to the database (COUNT + LIMIT/OFFSET)
    instead of loading every session through ``storage.list_sessions``.
    """
    from sqlalchemy import text

    from agentscope.app.storage import SessionRecord
    from bocomadp.pool_config import _get_engine

    engine = await _get_engine()

    # Total number of matching sessions
    async with engine.connect() as conn:
        total = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) FROM sessions "
                    "WHERE user_id = :user_id AND agent_id = :agent_id",
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )
        ).scalar_one()

    offset = (page - 1) * page_size

    # Current page of session records, newest-first
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, created_at, updated_at, user_id, agent_id, "
                    "source, source_schedule_id, team_id, payload "
                    "FROM sessions "
                    "WHERE user_id = :user_id AND agent_id = :agent_id "
                    "ORDER BY created_at DESC "
                    "LIMIT :limit OFFSET :offset",
                ),
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "limit": page_size,
                    "offset": offset,
                },
            )
        ).all()

    # Reconstruct full SessionRecord objects the same way the SQL storage
    # mapper does: merge the promoted columns back into ``payload`` and
    # let ``model_validate`` fire the record's validators.
    sessions: list[dict] = []
    for row in rows:
        obj: dict = dict(row.payload or {})
        obj["id"] = row.id
        obj["created_at"] = row.created_at
        obj["updated_at"] = row.updated_at
        obj["user_id"] = row.user_id
        obj["agent_id"] = row.agent_id
        obj["source"] = row.source
        obj["source_schedule_id"] = row.source_schedule_id
        obj["team_id"] = row.team_id
        sessions.append(
            SessionRecord.model_validate(obj).model_dump(mode="json")
        )

    return {
        "sessions": sessions,
        "total": total,
        "agent_id": agent_id,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(sessions) < total,
    }
