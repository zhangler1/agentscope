# -*- coding: utf-8 -*-
"""Persistent state for one orchestrated workflow run.

Only the authoritative index lives in MySQL. Large task inputs and full
node results live in the shared workspace; this table stores summaries and
paths so the payload stays small.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from bocomadp.team_store import _TeamBase

from .plan import WorkflowPlan

logger = logging.getLogger("bocomadp.team.state")

NodeStatus = Literal[
    "pending",
    "active",
    "waiting",
    "submitted",
    "completed",
    "interrupted",
    "failed",
]
FlowStatus = Literal["running", "completed", "failed", "cancelled"]
LeaderPhase = Literal[
    "first",
    "worker_failed",
    "final",
    "failed_summary",
]


class ResultRef(BaseModel):
    """Short summary plus an optional path to the full deliverable."""

    summary: str = ""
    path: str | None = None


class HandoffRef(BaseModel):
    """A submitted downstream task, stored as an index rather than full text."""

    target: str
    task: str = ""
    task_msg_id: str | None = None
    summary: str = ""
    inputs: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowNodeState(BaseModel):
    """Per-node execution state."""

    status: NodeStatus = "pending"
    session_id: str | None = None
    attempts: int = 0
    resume_count: int = 0
    task_msg_id: str | None = None
    task_summary: str = ""
    result: ResultRef | None = None
    handoffs: list[HandoffRef] = Field(default_factory=list)
    error: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    waiting_reason: str | None = None
    waiting_since: datetime | None = None
    last_heartbeat: datetime | None = None


class WorkflowLeaderState(BaseModel):
    """Leader run state (first / final / failed summary)."""

    phase: LeaderPhase = "first"
    session_id: str | None = None
    attempts: int = 0
    resume_count: int = 0
    last_result: ResultRef | None = None
    handoffs: list[HandoffRef] = Field(default_factory=list)
    last_error: str | None = None
    resume_requested: bool = False
    dispatched: bool = False
    failed_stage_index: int | None = None
    failed_nodes: list[str] = Field(default_factory=list)
    failed_errors: dict[str, str] = Field(default_factory=dict)


class WorkflowState(BaseModel):
    """One row of ``team_workflow_states``."""

    team_id: str
    user_id: str
    plan: WorkflowPlan
    stage_index: int = 0
    stage_members: list[str] = Field(default_factory=list)
    nodes: dict[str, WorkflowNodeState] = Field(default_factory=dict)
    leader: WorkflowLeaderState = Field(default_factory=WorkflowLeaderState)
    status: FlowStatus = "running"
    last_error: str | None = None
    truncated: bool = False
    version: int = 1

    def node(self, agent_id: str) -> WorkflowNodeState:
        node = self.nodes.get(agent_id)
        if node is None:
            node = WorkflowNodeState()
            self.nodes[agent_id] = node
        return node

    def current_members(self) -> list[str]:
        return list(self.stage_members)


class WorkflowStateRow(_TeamBase):
    """SQLAlchemy row; registered on the shared bocomadp metadata."""

    __tablename__ = "team_workflow_states"

    team_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_state(row: WorkflowStateRow) -> WorkflowState:
    return WorkflowState.model_validate(row.payload)


def _state_to_row(state: WorkflowState) -> dict[str, Any]:
    now = _now()
    return {
        "team_id": state.team_id,
        "user_id": state.user_id,
        "payload": state.model_dump(mode="json"),
        "version": state.version,
        "created_at": now,
        "updated_at": now,
    }


async def ensure_workflow_tables(storage: Any) -> None:
    """Create the workflow state table (idempotent).

    Importing this module registers :class:`WorkflowStateRow` on the shared
    ``_TeamBase`` metadata, so the existing ``ensure_team_tables`` call in
    ``main.py`` also creates it. This helper is kept for explicit startup
    ordering/tests.
    """
    from bocomadp.team_store import ensure_team_tables

    await ensure_team_tables(storage)
    logger.info("ensured table team_workflow_states")


async def get_state(storage: Any, team_id: str) -> WorkflowState | None:
    """Fetch one workflow state by team id."""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return None
    async with session_factory() as session:
        row = await session.get(WorkflowStateRow, team_id)
        return _row_to_state(row) if row is not None else None


async def upsert_state(storage: Any, state: WorkflowState) -> WorkflowState:
    """Insert state if missing; otherwise return the existing row untouched."""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return state
    async with session_factory() as session:
        row = await session.get(WorkflowStateRow, state.team_id)
        if row is not None:
            return _row_to_state(row)
        session.add(WorkflowStateRow(**_state_to_row(state)))
        await session.commit()
        return state


async def save_state(
    storage: Any,
    state: WorkflowState,
    expected_version: int | None = None,
) -> bool:
    """Optimistic-lock update.

    Returns ``False`` when the version no longer matches (the caller should
    reload, merge, and retry). On success ``state.version`` is incremented.
    """
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return False
    base_version = state.version if expected_version is None else expected_version
    next_version = base_version + 1
    payload = state.model_dump(mode="json")
    payload["version"] = next_version
    async with session_factory() as session:
        result = await session.execute(
            update(WorkflowStateRow)
            .where(
                WorkflowStateRow.team_id == state.team_id,
                WorkflowStateRow.version == base_version,
            )
            .values(
                payload=payload,
                version=next_version,
                updated_at=_now(),
            ),
        )
        await session.commit()
        if result.rowcount != 1:
            return False
    state.version = next_version
    return True


async def delete_state(storage: Any, team_id: str) -> None:
    """Delete the state row (team dissolved or cancelled)."""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return
    async with session_factory() as session:
        row = await session.get(WorkflowStateRow, team_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


async def list_states(storage: Any, user_id: str) -> list[WorkflowState]:
    """All workflow states for a user (diagnostics/tests)."""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return []
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(WorkflowStateRow).where(
                    WorkflowStateRow.user_id == user_id,
                ),
            )
        ).scalars().all()
        return [_row_to_state(row) for row in rows]


__all__ = [
    "FlowStatus",
    "HandoffRef",
    "LeaderPhase",
    "NodeStatus",
    "ResultRef",
    "WorkflowLeaderState",
    "WorkflowNodeState",
    "WorkflowState",
    "WorkflowStateRow",
    "delete_state",
    "ensure_workflow_tables",
    "get_state",
    "list_states",
    "save_state",
    "upsert_state",
]
