# -*- coding: utf-8 -*-
"""System-side orchestrator for the prescribed workflow.

The orchestrator owns routing: it creates the runtime team, mints member
sessions, dispatches task packages, watches submissions, advances stages,
and triggers the leader's final summary. Models never call team-routing
tools in this mode.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from agentscope.app._bus_ops import enqueue_run_trigger
from agentscope.app.message_bus import MessageBusKeys
from agentscope.app.storage import (
    SessionConfig,
    TeamData,
    TeamMember,
    TeamRecord,
)
from agentscope.message import HintBlock
from agentscope.state import AgentState

from bocomadp import team_store

from .event_filter import mark_workflow_session
from .plan import PlanError, build_plan
from .protocol import (
    build_continue_prompt,
    build_failed_prompt,
    build_results_prompt,
    build_task_prompt,
)
from .state import (
    WorkflowLeaderState,
    WorkflowNodeState,
    WorkflowState,
    delete_state,
    get_state,
    save_state,
    upsert_state,
)

logger = logging.getLogger("bocomadp.team.orchestrator")

MAX_ATTEMPTS = 3


@dataclass
class RunContext:
    """What the orchestrator needs to know after one chat run."""

    user_id: str
    session_id: str
    agent_id: str
    team_id: str
    role: str  # "leader" | "worker"
    phase: str


class WorkflowOrchestrator:
    """Owns one workflow per runtime team."""

    def __init__(
        self,
        *,
        storage: Any,
        message_bus: Any,
        session_service: Any | None = None,
        chat_service: Any | None = None,
    ) -> None:
        self._storage = storage
        self._message_bus = message_bus
        self._session_service = session_service
        self._chat_service = chat_service

    # ------------------------------------------------------------------
    # Run hooks
    # ------------------------------------------------------------------

    async def before_run(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        input_msg: Any = None,
    ) -> RunContext | None:
        """Ensure a plan/state exists and return the run context.

        Returns ``None`` for non-orchestrated sessions, so free_handoff and
        plain agents are untouched.
        """
        session = await self._storage.get_session(user_id, agent_id, session_id)
        if session is None:
            return None

        team = None
        if session.team_id:
            team = await self._storage.get_team(user_id, session.team_id)
        if team is not None:
            role = "leader" if team.session_id == session_id else "worker"
            state = await get_state(self._storage, team.id)
            if role == "leader" and state is None:
                state = await self.ensure_plan(
                    user_id=user_id,
                    leader_agent_id=agent_id,
                    session_id=session_id,
                )
            if state is None:
                return None
            return RunContext(
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                team_id=team.id,
                role=role,
                phase=state.leader.phase if role == "leader" else "",
            )

        # No runtime team yet: only a configured leader starts a workflow.
        rel = await team_store.get_team(self._storage, user_id, agent_id)
        if rel is None or rel.collaboration_mode != "workflow":
            return None
        state = await self.ensure_plan(
            user_id=user_id,
            leader_agent_id=agent_id,
            session_id=session_id,
        )
        if state is None:
            return None
        return RunContext(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            team_id=state.team_id,
            role="leader",
            phase=state.leader.phase,
        )

    async def after_run(self, ctx: RunContext | None) -> None:
        """Serialize completion handling per team (multi-replica safe)."""
        if ctx is None:
            return
        async with self._message_bus.acquire_lock(
            f"workflow_team:{ctx.team_id}",
            ttl_secs=300,
        ):
            await self._after_run_locked(ctx)

    async def _after_run_locked(self, ctx: RunContext) -> None:
        """React to a finished run: dispatch, retry, advance, or clean up."""
        state = await get_state(self._storage, ctx.team_id)
        if state is None:
            return

        if ctx.role == "leader":
            if state.leader.phase in ("final", "failed_summary"):
                await self.finish(state)
                return
            if state.leader.resume_requested:
                await self.resume(state)
                return
            if state.leader.dispatched:
                # Already running: user messages during this period are not
                # new dispatch requests.
                return
            if state.leader.handoffs:
                state.leader.dispatched = True
                await save_state(self._storage, state)
                await self.dispatch_stage(state, 0)
                return
            if not state.leader.handoffs:
                state.leader.attempts += 1
                if state.leader.attempts < MAX_ATTEMPTS:
                    state.leader.last_error = (
                        "leader first run ended without SubmitHandoffs"
                    )
                    await save_state(self._storage, state)
                    await self._push_and_wake(
                        state.user_id,
                        ctx.session_id,
                        ctx.agent_id,
                        "上一轮没有提交 handoffs。请调用 SubmitHandoffs 提交 "
                        "result（≤500 字）和给当前阶段每个成员的 handoffs；"
                        "不要重新开始对话。",
                    )
                else:
                    state.status = "failed"
                    state.last_error = "leader 首节点连续 3 次未提交 handoffs"
                    await save_state(self._storage, state)
                    logger.error(
                        "leader first failed after %d attempts for team %s",
                        state.leader.attempts,
                        ctx.team_id,
                    )
            return

        # Worker run ended.
        node = state.nodes.get(ctx.agent_id)
        if node is None:
            return
        if node.status in ("completed", "submitted"):
            await self._check_stage_done(state)
            return
        if node.status == "interrupted":
            return
        if node.status == "failed":
            return
        if node.status != "active":
            return

        node.error = "节点 run 结束但未调用 SubmitHandoffs"
        if node.attempts >= MAX_ATTEMPTS:
            node.status = "failed"
            await save_state(self._storage, state)
            await self._trigger_failed_summary(state)
            return
        node.status = "pending"
        await save_state(self._storage, state)
        await self.dispatch_stage(state, state.stage_index)

    # ------------------------------------------------------------------
    # Plan / state initialization
    # ------------------------------------------------------------------

    async def ensure_plan(
        self,
        *,
        user_id: str,
        leader_agent_id: str,
        session_id: str,
    ) -> WorkflowState | None:
        """Create the runtime team + initial workflow state for a leader."""
        rel = await team_store.get_team(self._storage, user_id, leader_agent_id)
        if rel is None or rel.collaboration_mode != "workflow":
            return None
        try:
            plan = build_plan(rel, leader_agent_id)
        except PlanError as exc:
            logger.warning(
                "cannot start workflow for leader %s: %s",
                leader_agent_id,
                exc,
            )
            return None

        session = await self._storage.get_session(
            user_id,
            leader_agent_id,
            session_id,
        )
        if session is None:
            return None

        team = None
        if session.team_id:
            team = await self._storage.get_team(user_id, session.team_id)
        if team is None:
            agent = await self._storage.get_agent(user_id, leader_agent_id)
            name = (
                f"{agent.data.name} 专家团"
                if agent is not None
                else "专家团"
            )
            team = TeamRecord(
                user_id=user_id,
                session_id=session_id,
                data=TeamData(
                    name=name,
                    description="用户指定工作流的运行时团队",
                    member_ids=[],
                ),
            )
            await self._storage.upsert_team(user_id, team)
            await self._storage.set_session_team_id(
                user_id,
                session_id,
                team.id,
            )
        else:
            existing = await get_state(self._storage, team.id)
            if existing is not None:
                return existing

        state = WorkflowState(
            team_id=team.id,
            user_id=user_id,
            plan=plan,
            stage_index=0,
            stage_members=plan.members_of(0),
            nodes={member: WorkflowNodeState() for member in plan.nodes},
            leader=WorkflowLeaderState(
                phase="first",
                session_id=session_id,
            ),
        )
        return await upsert_state(self._storage, state)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch_stage(
        self,
        state: WorkflowState,
        stage_index: int,
    ) -> None:
        """Mint sessions and deliver tasks for one stage."""
        members = state.plan.members_of(stage_index)
        if not members:
            await self._trigger_leader_final(state)
            return

        deliveries: list[tuple[str, str, str]] = []
        for agent_id in members:
            node = state.node(agent_id)
            if node.status in ("completed", "submitted"):
                continue
            session_id = await self._ensure_member_session(
                state,
                agent_id,
            )
            if session_id is None:
                node.status = "failed"
                node.attempts = MAX_ATTEMPTS
                node.error = "无法创建成员会话"
                continue
            task, inputs = self._collect_task(state, agent_id)
            node.status = "active"
            node.session_id = session_id
            node.attempts += 1
            node.task_summary = task[:200]
            prompt = build_task_prompt(
                task,
                inputs,
                stage_note=(
                    f"当前是第 {stage_index + 1}/{state.plan.stage_count} 阶段"
                ),
            )
            deliveries.append((session_id, agent_id, prompt))

        await save_state(self._storage, state)
        for session_id, agent_id, prompt in deliveries:
            await self._push_and_wake(
                state.user_id,
                session_id,
                agent_id,
                prompt,
            )
        if not deliveries:
            await self._check_stage_done(state)

    async def _ensure_member_session(
        self,
        state: WorkflowState,
        agent_id: str,
    ) -> str | None:
        """Create or reuse the team-scoped session for a member."""
        team = await self._storage.get_team(state.user_id, state.team_id)
        if team is None:
            return None
        leader_agent_id = state.plan.leader_agent_id
        leader_session = await self._storage.get_session(
            state.user_id,
            leader_agent_id,
            team.session_id,
        )
        if leader_session is None:
            return None

        name = f"team:{team.id}/orchestrated:{agent_id}"
        for existing in await self._storage.list_sessions(
            state.user_id,
            agent_id,
        ):
            if (existing.config.name or "") == name:
                return existing.id

        worker_state = AgentState()
        leader_state = getattr(leader_session, "state", None)
        if leader_state is not None:
            worker_state.permission_context = (
                leader_state.permission_context.model_copy(deep=True)
            )
        session = await self._storage.upsert_session(
            user_id=state.user_id,
            agent_id=agent_id,
            config=SessionConfig(
                workspace_id=leader_session.config.workspace_id,
                name=name,
                chat_model_config=leader_session.config.chat_model_config,
                fallback_chat_model_config=(
                    leader_session.config.fallback_chat_model_config
                ),
            ),
            state=worker_state,
        )
        await self._storage.set_session_team_id(
            state.user_id,
            session.id,
            team.id,
        )
        if not any(m.agent_id == agent_id for m in team.data.members):
            team.data.members.append(
                TeamMember(
                    owner_id=state.user_id,
                    agent_id=agent_id,
                    session_id=session.id,
                    role="invited",
                ),
            )
            await self._storage.upsert_team(state.user_id, team)
        return session.id

    def _collect_task(
        self,
        state: WorkflowState,
        agent_id: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Collect task text + inputs from the previous stage's handoffs."""
        if state.stage_index == 0:
            refs = list(state.leader.handoffs)
            previous_members: list[str] = []
        else:
            previous_members = state.plan.members_of(state.stage_index - 1)
            refs = []
            for member in previous_members:
                refs.extend(state.node(member).handoffs)

        matches = [ref for ref in refs if ref.target == agent_id]
        if not matches:
            inputs: list[dict[str, Any]] = []
            for member in previous_members:
                result = state.node(member).result
                if result and result.path:
                    inputs.append({"type": "file", "path": result.path})
            return (
                "基于上游结果继续完成你的角色任务。",
                inputs,
            )

        if len(matches) == 1:
            ref = matches[0]
            return ref.task or ref.summary, list(ref.inputs)

        parts: list[str] = []
        inputs: list[dict[str, Any]] = []
        for ref in matches:
            parts.append(f"## 来自上游的任务\n{ref.task or ref.summary}")
            inputs.extend(ref.inputs)
        return "\n\n".join(parts), inputs

    # ------------------------------------------------------------------
    # Stage completion / final summary
    # ------------------------------------------------------------------

    async def _check_stage_done(self, state: WorkflowState) -> None:
        # Idempotency: every completed worker's after_run calls this method.
        # Once the flow is finishing/finished, later calls must not enqueue a
        # second final/failed-summary hint.
        if state.status != "running":
            return
        if state.leader.phase in ("final", "failed_summary"):
            return
        if not state.stage_members:
            return
        statuses = [
            state.node(member).status for member in state.stage_members
        ]
        if all(status in ("completed", "submitted") for status in statuses):
            next_index = state.stage_index + 1
            if next_index >= state.plan.stage_count:
                await self._trigger_leader_final(state)
                return
            state.stage_index = next_index
            state.stage_members = state.plan.members_of(next_index)
            for member in state.stage_members:
                state.node(member).status = "pending"
            await save_state(self._storage, state)
            await self.dispatch_stage(state, next_index)
            return

        failed = [
            member
            for member in state.stage_members
            if state.node(member).status == "failed"
        ]
        if failed and all(
            state.node(member).attempts >= MAX_ATTEMPTS
            for member in failed
        ):
            await self._trigger_failed_summary(state)

    async def _trigger_leader_final(self, state: WorkflowState) -> bool:
        # Re-read under the team lock so an older caller cannot enqueue a
        # second final hint after another worker already triggered it.
        fresh = await get_state(self._storage, state.team_id)
        if fresh is None or fresh.leader.phase in (
            "final",
            "failed_summary",
        ):
            return False
        team = await self._storage.get_team(fresh.user_id, fresh.team_id)
        if team is None:
            return False
        leader_session = await self._storage.get_session(
            fresh.user_id,
            fresh.plan.leader_agent_id,
            team.session_id,
        )
        if leader_session is None:
            return False
        results = self._collect_results(fresh)
        fresh.leader.phase = "final"
        fresh.status = "running"
        if not await save_state(self._storage, fresh):
            return False
        await self._push_and_wake(
            fresh.user_id,
            leader_session.id,
            fresh.plan.leader_agent_id,
            build_results_prompt(results),
        )
        return True

    async def _trigger_failed_summary(self, state: WorkflowState) -> bool:
        fresh = await get_state(self._storage, state.team_id)
        if fresh is None or fresh.leader.phase in (
            "final",
            "failed_summary",
        ):
            return False
        team = await self._storage.get_team(fresh.user_id, fresh.team_id)
        if team is None:
            return False
        leader_session = await self._storage.get_session(
            fresh.user_id,
            fresh.plan.leader_agent_id,
            team.session_id,
        )
        if leader_session is None:
            return False
        results = self._collect_results(fresh)
        errors = [
            {
                "agent": member,
                "attempts": fresh.node(member).attempts,
                "error": fresh.node(member).error,
            }
            for member in fresh.stage_members
            if fresh.node(member).status == "failed"
        ]
        fresh.leader.phase = "failed_summary"
        fresh.status = "failed"
        if not await save_state(self._storage, fresh):
            return False
        await self._push_and_wake(
            fresh.user_id,
            leader_session.id,
            fresh.plan.leader_agent_id,
            build_failed_prompt(results, errors),
        )
        return True

    def _collect_results(self, state: WorkflowState) -> list[dict[str, Any]]:
        stage_of: dict[str, int] = {}
        for stage in state.plan.stages:
            for member in stage.members:
                stage_of[member] = stage.stage
        results: list[dict[str, Any]] = []
        for member in state.plan.nodes:
            node = state.nodes.get(member)
            if node is None or node.result is None:
                continue
            results.append(
                {
                    "from": member,
                    "stage": stage_of.get(member),
                    "summary": node.result.summary,
                    "path": node.result.path,
                },
            )
        return results

    # ------------------------------------------------------------------
    # Resume / cancel / cleanup
    # ------------------------------------------------------------------

    async def resume(self, state: WorkflowState) -> None:
        """Context-level resume; never re-dispatches a full original task."""
        state.leader.resume_requested = False
        if state.leader.phase in ("final", "failed_summary"):
            # Final summary is not re-routed; clearing the flag avoids a
            # resume loop if the model called ResumeWorkflow late.
            await save_state(self._storage, state)
            return

        if state.leader.phase == "first" and not state.leader.handoffs:
            team = await self._storage.get_team(state.user_id, state.team_id)
            leader_session_id = (
                state.leader.session_id
                or (team.session_id if team is not None else None)
            )
            await save_state(self._storage, state)
            if leader_session_id:
                await self._push_and_wake(
                    state.user_id,
                    leader_session_id,
                    state.plan.leader_agent_id,
                    "请继续上次的首节点拆解，并调用 SubmitHandoffs 提交 "
                    "handoffs；不要重新开始整个对话。",
                )
            return

        if (
            state.leader.phase == "first"
            and state.leader.handoffs
            and not state.leader.dispatched
        ):
            state.leader.dispatched = True
            await save_state(self._storage, state)
            await self.dispatch_stage(state, 0)
            return

        interrupted = [
            member
            for member, node in state.nodes.items()
            if node.status in ("interrupted", "active")
        ]
        if not interrupted:
            await save_state(self._storage, state)
            return

        for member in interrupted:
            node = state.node(member)
            if node.status == "completed":
                continue
            session_id = node.session_id or await self._ensure_member_session(
                state,
                member,
            )
            if session_id is None:
                continue
            node.status = "active"
            node.resume_count += 1
            node.session_id = session_id
            await self._push_and_wake(
                state.user_id,
                session_id,
                member,
                build_continue_prompt(
                    node.task_summary,
                    node.result.path if node.result else None,
                ),
            )
        state.leader.resume_count += 1
        await save_state(self._storage, state)

    async def cancel(self, user_id: str, team_id: str) -> bool:
        """Cancel a running workflow and dissolve the runtime team."""
        async with self._message_bus.acquire_lock(
            f"workflow_team:{team_id}",
            ttl_secs=300,
        ):
            state = await get_state(self._storage, team_id)
            if state is None:
                return False
            state.status = "cancelled"
            await save_state(self._storage, state)
            await self._dissolve(user_id, team_id)
            return True

    async def finish(self, state: WorkflowState) -> None:
        """Normal completion: dissolve the runtime team, keep the workspace."""
        await self._dissolve(state.user_id, state.team_id)

    async def _dissolve(self, user_id: str, team_id: str) -> None:
        try:
            if self._session_service is not None:
                await self._session_service.delete_team(user_id, team_id)
            else:
                await self._storage.delete_team(user_id, team_id)
        except Exception:  # pylint: disable=broad-except
            logger.exception("failed to dissolve workflow team %s", team_id)
        await delete_state(self._storage, team_id)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _push_and_wake(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        text: str,
    ) -> None:
        mark_workflow_session(session_id)
        hint = HintBlock(
            hint=text,
            source=json.dumps(
                {"label": "workflow_task", "sublabel": "orchestrator"},
                ensure_ascii=False,
            ),
        )
        await self._message_bus.queue_push(
            MessageBusKeys.inbox(session_id),
            hint.model_dump(mode="json"),
        )
        await enqueue_run_trigger(
            self._message_bus,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
        )


__all__ = ["RunContext", "WorkflowOrchestrator"]
