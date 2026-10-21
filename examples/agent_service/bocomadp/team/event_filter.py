# -*- coding: utf-8 -*-
"""Hide internal workflow tool events from the user-facing SSE stream.

``SubmitHandoffs`` / ``ResumeWorkflow`` are control-plane tools. Their call
and result frames would show up as tool cards in the UI, so they are
filtered here while the structured payload still lands in the state table.
"""
from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger("bocomadp.team.event_filter")

WORKFLOW_SESSIONS: set[str] = set()
_INTERNAL_TOOLS = {"SubmitHandoffs", "ResumeWorkflow"}
_internal_calls: dict[str, set[str]] = defaultdict(set)

_patched = False
_original: object | None = None


def mark_workflow_session(session_id: str) -> None:
    if session_id:
        WORKFLOW_SESSIONS.add(session_id)


def unmark_workflow_session(session_id: str) -> None:
    WORKFLOW_SESSIONS.discard(session_id)
    _internal_calls.pop(session_id, None)


def _should_skip(session_id: str, event: dict) -> bool:
    if session_id not in WORKFLOW_SESSIONS:
        return False
    event_type = str(event.get("type") or "")
    call_id = str(event.get("tool_call_id") or "")
    if not call_id:
        return False
    calls = _internal_calls[session_id]
    if event_type == "TOOL_CALL_START":
        if event.get("tool_call_name") in _INTERNAL_TOOLS:
            calls.add(call_id)
            return True
        return False
    if call_id in calls:
        if event_type == "TOOL_RESULT_END":
            calls.discard(call_id)
        return True
    return False


def patch_event_filter() -> None:
    """Wrap ``_chat.publish_session_event``; idempotent."""
    global _patched, _original
    if _patched:
        return
    from agentscope.app._service import _chat as _chat_module

    _original = _chat_module.publish_session_event

    async def _filtered(bus, session_id, event, *, run_id=None):
        if _should_skip(session_id, event):
            return ""
        return await _original(bus, session_id, event, run_id=run_id)

    _chat_module.publish_session_event = _filtered
    _patched = True
    logger.info("patched publish_session_event with workflow tool filter")


__all__ = [
    "WORKFLOW_SESSIONS",
    "mark_workflow_session",
    "patch_event_filter",
    "unmark_workflow_session",
]
