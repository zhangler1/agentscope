# -*- coding: utf-8 -*-
"""Internal tools used by orchestrated workflow nodes.

``SubmitHandoffs`` is the only way a node reports its result and downstream
tasks. ``ResumeWorkflow`` lets the leader model express a "continue" intent;
the system performs the actual resume.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ParamsBase, ToolBase, ToolChunk

from .protocol import (
    RESULT_MAX_CHARS,
    normalize_result,
    result_is_too_long,
)
from .state import (
    HandoffRef,
    ResultRef,
    WorkflowState,
    get_state,
    save_state,
)

logger = logging.getLogger("bocomadp.team.tools")


class _HandoffItem(ParamsBase):
    target: str = Field(
        description="计划中的直接下游 agent id；必须是系统当前允许的目标。",
    )
    task: str = Field(
        description="自包含的任务指令：下游只读该字段也能执行。",
    )
    inputs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="可选输入；大段内容优先写共享文件，这里放文件路径。",
    )


class _SubmitParams(ParamsBase):
    result: str = Field(
        description=(
            "给 leader 的简短汇报（≤500 字）：结论、关键数据、完成状态。"
            "完整产物请写入共享工作区并用 result_path 指向。"
        ),
    )
    handoffs: list[_HandoffItem] = Field(
        default_factory=list,
        description="有下游时必填；最后一阶段/最终答复可以为空。",
    )
    result_path: str | None = Field(
        default=None,
        description="可选：完整交付物在共享工作区中的路径。",
    )


class _ResumeParams(ParamsBase):
    reason: str = Field(
        default="",
        description="可选：说明为什么继续。",
    )


class _GetStatusParams(ParamsBase):
    """GetWorkflowStatus takes no parameters."""


class _WorkflowToolBase(ToolBase):
    """Base class for internal workflow tools (always allowed)."""

    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name: str | None = None

    def __init__(
        self,
        *,
        storage: Any,
        user_id: str,
        session_id: str,
        agent_id: str,
        team_id: str | None = None,
        session_service: Any | None = None,
        message_bus: Any | None = None,
    ) -> None:
        self._storage = storage
        self._user_id = user_id
        self._session_id = session_id
        self._agent_id = agent_id
        self._team_id = team_id
        self._session_service = session_service
        if self._session_service is None and message_bus is not None:
            from agentscope.app._service import SessionService

            self._session_service = SessionService(
                self._storage,
                message_bus,
            )
        self._message_bus = message_bus

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed for workflow nodes.",
        )

    async def _load_state(self) -> WorkflowState | None:
        team_id = self._team_id
        if not team_id:
            return None
        return await get_state(self._storage, team_id)

    async def _save_state(self, state: WorkflowState) -> bool:
        """Save with a small optimistic-lock retry loop."""
        team_id = self._team_id
        if not team_id:
            return False
        for _ in range(3):
            if await save_state(self._storage, state):
                return True
            fresh = await get_state(self._storage, team_id)
            if fresh is None:
                return False
            state.version = fresh.version
        return False


class SubmitHandoffs(_WorkflowToolBase):
    """Submit a node's short result and downstream task packages."""

    name = "SubmitHandoffs"
    description = (
        "提交本节点的结果和给下游的任务。完成任务后必须调用此工具。\n"
        "result 是给 leader 的简短汇报（≤500 字），不是完整产物；"
        "完整产物写到共享工作区并用 result_path 指向。\n"
        "handoffs 只能指向系统允许的直接下游；没有下游时留空。"
    )
    input_schema = _SubmitParams.model_json_schema()

    def __init__(
        self,
        *,
        is_leader: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._is_leader = is_leader

    async def __call__(
        self,
        result: str,
        handoffs: list[dict[str, Any]] | None = None,
        result_path: str | None = None,
    ) -> ToolChunk:
        text = (result or "").strip()
        if not text:
            return _error("SubmitHandoffs: result 不能为空。")
        if result_is_too_long(text):
            return _error(
                f"SubmitHandoffs: result 超过 {RESULT_MAX_CHARS} 字，"
                "请精简为结论/关键数据；完整内容写入共享工作区并用 "
                "result_path 指向。",
            )

        state = await self._load_state()
        if state is None:
            return _error(
                "SubmitHandoffs: 当前团队没有 workflow 状态，"
                "请确认流程已启动。",
            )
        if state.status not in ("running", "failed"):
            return _error(
                f"SubmitHandoffs: 流程状态为 {state.status}，不能提交。",
            )

        if self._is_leader:
            if state.leader.dispatched:
                return _error(
                    "SubmitHandoffs: 当前流程已经派发，不能重新拆解；"
                    "如需新任务请先取消当前流程。",
                )
            if state.leader.handoffs:
                return _error(
                    "SubmitHandoffs: 本轮已经提交过 handoffs，不能重复提交。",
                )
            allowed = state.plan.members_of(0)
            stage_note = "leader 首节点"
        else:
            node = state.nodes.get(self._agent_id)
            if node is None or node.status not in (
                "active",
                "waiting",
                "interrupted",
                "failed",
            ):
                return _error(
                    "SubmitHandoffs: 当前节点不是活动节点，或已经提交过。",
                )
            if self._agent_id not in state.stage_members:
                return _error(
                    "SubmitHandoffs: 当前节点不属于当前阶段。",
                )
            allowed = state.plan.members_of(state.stage_index + 1)
            stage_note = f"阶段 {state.stage_index + 1}"

        targets = [str(item.get("target") or "") for item in (handoffs or [])]
        if allowed:
            missing = [target for target in allowed if target not in targets]
            unknown = [target for target in targets if target not in allowed]
            if missing or unknown:
                details = []
                if unknown:
                    details.append(f"计划外目标：{unknown}")
                if missing:
                    details.append(f"缺少目标：{missing}")
                return _error(
                    "SubmitHandoffs: handoffs 与系统分配的下游不一致（"
                    + "；".join(details)
                    + f"）。{stage_note} 允许的目标：{allowed}。"
                    "请修正后重新提交。",
                )
        elif targets:
            return _error(
                "SubmitHandoffs: 当前是最后一步，handoffs 必须为空。",
            )

        for item in handoffs or []:
            if not str(item.get("task") or "").strip():
                return _error(
                    f"SubmitHandoffs: target={item.get('target')!r} 的 task 不能为空。",
                )

        refs = [
            HandoffRef(
                target=str(item.get("target") or ""),
                task=str(item.get("task") or ""),
                summary=str(item.get("task") or "")[:200],
                inputs=list(item.get("inputs") or []),
            )
            for item in (handoffs or [])
        ]
        result_ref = ResultRef(
            summary=normalize_result(text),
            path=result_path,
        )

        completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        team_id = self._team_id
        if not team_id:
            return _error("SubmitHandoffs: 当前团队状态不存在。")
        # Merge-on-conflict: fan-in nodes submit concurrently, so a stale
        # whole-row write would drop another node's result.
        for _ in range(3):
            fresh = await get_state(self._storage, team_id)
            if fresh is None:
                return _error("SubmitHandoffs: 状态已消失。")
            if self._is_leader:
                if fresh.leader.dispatched or fresh.leader.handoffs:
                    return _error(
                        "SubmitHandoffs: handoffs 已经提交或流程已派发。",
                    )
                fresh.leader.last_result = result_ref
                fresh.leader.handoffs = refs
            else:
                fresh_node = fresh.nodes.get(self._agent_id)
                if (
                    fresh_node is None
                    or fresh_node.status
                    not in ("active", "waiting", "interrupted", "failed")
                ):
                    return _error(
                        "SubmitHandoffs: 节点状态已变化，请刷新后重试。",
                    )
                fresh_node.status = "completed"
                fresh_node.result = result_ref
                fresh_node.handoffs = refs
                fresh_node.completed_at = completed_at
            if await save_state(self._storage, fresh):
                return ToolChunk(
                    content=[TextBlock(text="SubmitHandoffs: 已提交。")],
                    state=ToolResultState.SUCCESS,
                )
        return _error("SubmitHandoffs: 状态写入冲突，请重试。")


class ResumeWorkflow(_WorkflowToolBase):
    """Signal that the user wants to continue an interrupted workflow."""

    name = "ResumeWorkflow"
    description = (
        "当用户明确表示继续上次未完成的任务时调用。只表达意图；"
        "系统会决定恢复哪个阶段。新任务不要调用。"
    )
    input_schema = _ResumeParams.model_json_schema()

    async def __call__(self, reason: str = "") -> ToolChunk:
        state = await self._load_state()
        if state is None:
            return _error("ResumeWorkflow: 当前没有可恢复的流程。")
        if state.status not in ("running", "failed"):
            return _error(
                f"ResumeWorkflow: 流程状态为 {state.status}，无法恢复。",
            )
        team_id = self._team_id
        if not team_id:
            return _error("ResumeWorkflow: 当前团队状态不存在。")
        for _ in range(3):
            fresh = await get_state(self._storage, team_id)
            if fresh is None:
                return _error("ResumeWorkflow: 状态已消失。")
            fresh.leader.resume_requested = True
            fresh.leader.resume_count += 1
            if await save_state(self._storage, fresh):
                return ToolChunk(
                    content=[
                        TextBlock(text="ResumeWorkflow: 已记录继续请求。"),
                    ],
                    state=ToolResultState.SUCCESS,
                )
        return _error("ResumeWorkflow: 状态写入冲突，请重试。")


class GetWorkflowStatus(_WorkflowToolBase):
    """Read-only view of the current team's workflow status."""

    name = "GetWorkflowStatus"
    description = (
        "查询当前团队和成员的真实执行状态。只读，不会触发成员运行。"
        "返回 active / waiting / completed / failed 等状态。"
        "如果返回 active 或 waiting，说明成员仍在执行或等待，不能判断失败，"
        "也不要连续调用本工具轮询；本轮应结束，等待系统唤醒。"
    )
    input_schema = _GetStatusParams.model_json_schema()
    is_read_only = True
    is_concurrency_safe = True

    async def _resolve_team_id(self) -> str | None:
        if self._team_id:
            return self._team_id
        try:
            session = await self._storage.get_session(
                self._user_id,
                self._agent_id,
                self._session_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "GetWorkflowStatus failed to resolve session %s",
                self._session_id,
            )
            return None
        return session.team_id if session is not None else None

    async def _session_status(
        self,
        agent_id: str,
        session_id: str | None,
    ) -> str | None:
        if self._session_service is None or not session_id:
            return None
        try:
            status = await self._session_service.get_session_status(
                self._user_id,
                agent_id,
                session_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "GetWorkflowStatus failed to read session %s",
                session_id,
            )
            return "unknown"
        return getattr(status, "value", str(status))

    async def _name(self, agent_id: str) -> str:
        try:
            record = await self._storage.get_agent(self._user_id, agent_id)
        except Exception:  # pylint: disable=broad-except
            record = None
        if record is None:
            return agent_id
        return record.data.name

    async def _render_workflow(self, state: WorkflowState) -> str:
        lines = [
            f"团队状态：{state.status}",
            "模式：workflow",
            f"阶段：{state.stage_index + 1}/{state.plan.stage_count}",
            f"leader 阶段：{state.leader.phase}",
            "成员：",
        ]
        counts = {
            "completed": 0,
            "active": 0,
            "waiting": 0,
            "failed": 0,
            "pending": 0,
        }
        for agent_id, node in state.nodes.items():
            name = await self._name(agent_id)
            detail = f"- {name} ({agent_id})：{node.status}"
            if node.waiting_reason:
                detail += f" [{node.waiting_reason}]"
            detail += f"，attempts={node.attempts}"
            if node.result and node.result.summary:
                detail += f"，result={node.result.summary[:120]}"
            lines.append(detail)
            counts[node.status] = counts.get(node.status, 0) + 1

        lines.append(
            "汇总："
            f"completed={counts['completed']}，"
            f"active={counts['active']}，"
            f"waiting={counts['waiting']}，"
            f"failed={counts['failed']}，"
            f"pending={counts['pending']}",
        )
        if state.status == "completed":
            lines.append("结论：流程已完成。")
        elif state.status == "failed":
            errors = [
                f"{await self._name(agent_id)}：{node.error or '未知错误'}"
                for agent_id, node in state.nodes.items()
                if node.status == "failed"
            ]
            lines.append("结论：流程已失败。" + "；".join(errors))
        elif counts["active"] or counts["waiting"]:
            lines.append("结论：成员仍在执行或等待，尚未完成。")
        else:
            lines.append("结论：流程仍在进行，但当前没有活跃成员。")
        return "\n".join(lines)

    async def _render_free_handoff(self, team: Any) -> str:
        lines = ["团队状态：running", "模式：free_handoff"]
        leader_session = await self._storage.get_session(
            self._user_id,
            "",
            team.session_id,
        )
        if leader_session is not None:
            status = await self._session_status(
                leader_session.agent_id,
                leader_session.id,
            )
            lines.append(
                f"- leader {await self._name(leader_session.agent_id)}"
                f"：{status or 'unknown'}",
            )
        for member in getattr(team.data, "members", []) or []:
            status = await self._session_status(
                member.agent_id,
                getattr(member, "session_id", None),
            )
            lines.append(
                f"- {await self._name(member.agent_id)}"
                f" ({member.agent_id})：{status or 'unknown'}",
            )
        lines.append(
            "结论：free_handoff 没有阶段状态，成员当前状态如上。",
        )
        return "\n".join(lines)

    async def __call__(self) -> ToolChunk:
        team_id = await self._resolve_team_id()
        if not team_id:
            return ToolChunk(
                content=[TextBlock(text="GetWorkflowStatus: 当前没有活动团队。")],
                state=ToolResultState.SUCCESS,
            )
        state = await get_state(self._storage, team_id)
        if state is not None:
            text = await self._render_workflow(state)
        else:
            team = await self._storage.get_team(self._user_id, team_id)
            if team is None:
                return ToolChunk(
                    content=[TextBlock(text="GetWorkflowStatus: 当前没有活动团队。")],
                    state=ToolResultState.SUCCESS,
                )
            text = await self._render_free_handoff(team)
        return ToolChunk(
            content=[TextBlock(text=text)],
            state=ToolResultState.SUCCESS,
        )


def _error(message: str) -> ToolChunk:
    return ToolChunk(
        content=[TextBlock(text=message)],
        state=ToolResultState.ERROR,
    )


__all__ = ["GetWorkflowStatus", "ResumeWorkflow", "SubmitHandoffs"]
