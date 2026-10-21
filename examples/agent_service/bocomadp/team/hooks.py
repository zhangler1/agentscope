# -*- coding: utf-8 -*-
"""Runtime patches that connect the orchestrator to ChatService."""
from __future__ import annotations

import logging
from typing import Any

from .event_filter import mark_workflow_session, patch_event_filter
from .orchestrator import RunContext, WorkflowOrchestrator

logger = logging.getLogger("bocomadp.team.hooks")

_orchestrator: WorkflowOrchestrator | None = None
_original_run: Any = None
_patched_run = False


def set_orchestrator(orchestrator: WorkflowOrchestrator | None) -> None:
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> WorkflowOrchestrator | None:
    return _orchestrator


_legacy_guard_patched = False


def patch_legacy_team_tools() -> None:
    """Reject AgentCreate/TeamCreate when the session is orchestrated.

    The tools are normally not attached in orchestrated mode; this guard is
    a belt-and-braces protection for any future attachment path.
    """
    global _legacy_guard_patched
    if _legacy_guard_patched:
        return
    from agentscope.app._tool import AgentCreate, TeamCreate
    from agentscope.message import TextBlock, ToolResultState
    from agentscope.tool import ToolChunk

    async def _guard(tool: Any, *args: Any, **kwargs: Any) -> Any:
        from bocomadp import team_store

        try:
            storage = tool._storage
            user_id = tool._user_id
            session_id = tool._session_id
            agent_id = tool._agent_id
            session = await storage.get_session(
                user_id,
                agent_id,
                session_id,
            )
            team_id = session.team_id if session is not None else None
            if team_id:
                team = await storage.get_team(user_id, team_id)
                if team is not None:
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
                        if (
                            rel is not None
                            and rel.collaboration_mode == "workflow"
                        ):
                            return ToolChunk(
                                content=[
                                    TextBlock(
                                        text=(
                                            f"{tool.name}: 编排模式下禁止创建"
                                            "新成员或新团队。"
                                        ),
                                    ),
                                ],
                                state=ToolResultState.ERROR,
                            )
            rel = await team_store.get_team(storage, user_id, agent_id)
            if rel is not None and rel.collaboration_mode == "workflow":
                return ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                f"{tool.name}: 编排模式下禁止创建新成员"
                                "或新团队。"
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                )
        except Exception:  # pylint: disable=broad-except
            logger.exception("legacy team-tool guard failed")
        return await tool._workflow_original(*args, **kwargs)

    for cls in (AgentCreate, TeamCreate):
        if getattr(cls, "_workflow_original", None) is None:
            cls._workflow_original = cls.__call__
            cls.__call__ = _guard  # type: ignore[method-assign]
    _legacy_guard_patched = True
    logger.info("patched AgentCreate/TeamCreate with workflow guards")


def patch_chat_service_run() -> None:
    """Wrap ``ChatService.run`` with before/after orchestration hooks."""
    global _original_run, _patched_run
    if _patched_run:
        return
    from agentscope.app._service._chat import ChatService

    _original_run = ChatService.run

    async def _run(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        input_msg: Any = None,
        run_id: str | None = None,
    ) -> None:
        orchestrator = get_orchestrator()
        ctx: RunContext | None = None
        if orchestrator is not None:
            try:
                ctx = await orchestrator.before_run(
                    user_id,
                    session_id,
                    agent_id,
                    input_msg,
                )
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "workflow before_run failed for session %s",
                    session_id,
                )
            if ctx is not None:
                mark_workflow_session(session_id)
        try:
            return await _original_run(
                self,
                user_id,
                session_id,
                agent_id,
                input_msg,
                run_id,
            )
        finally:
            if orchestrator is not None and ctx is not None:
                try:
                    await orchestrator.after_run(ctx)
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "workflow after_run failed for session %s",
                        session_id,
                    )

    ChatService.run = _run  # type: ignore[method-assign]
    _patched_run = True
    patch_event_filter()
    logger.info("patched ChatService.run with workflow orchestrator hooks")


__all__ = [
    "get_orchestrator",
    "patch_chat_service_run",
    "set_orchestrator",
]
