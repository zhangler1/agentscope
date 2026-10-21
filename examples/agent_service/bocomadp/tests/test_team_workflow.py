# -*- coding: utf-8 -*-
"""Tests for the orchestrated workflow core (plan/state/protocol/tools)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bocomadp import team_store
from bocomadp.team.plan import PlanError, build_plan
from bocomadp.team.protocol import (
    RESULT_MAX_CHARS,
    build_task_prompt,
    estimate_tokens,
    truncate_text,
)
from bocomadp.team.state import (
    WorkflowLeaderState,
    WorkflowNodeState,
    WorkflowState,
    delete_state,
    get_state,
    save_state,
    upsert_state,
)
from bocomadp.team.tools import SubmitHandoffs
from bocomadp.team.workspace import TeamWorkspaceManager
from bocomadp.team_store import ExpertTeamMember, ExpertTeamRelation, HandoffRelation


def _run(coro):
    return asyncio.run(coro)


def _rel(edges, members=("a", "b", "c", "d")):
    return ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="workflow",
        members=[ExpertTeamMember(agent_id=m) for m in members],
        handoff_relations=[
            HandoffRelation(from_agent_id=source, to_agent_id=target)
            for source, target in edges
        ],
    )


@pytest.fixture
def workflow_storage(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workflow.db'}",
    )

    class _FakeStorage:
        async def get_session(self, user_id, agent_id, session_id):
            return self.sessions.get(session_id)

        async def get_agent(self, user_id, agent_id):
            return None

    storage = _FakeStorage()
    storage._engine = engine
    storage._session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    storage.sessions = {}
    storage.teams = {}
    return storage


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_build_plan_fanout():
    plan = build_plan(
        _rel([("leader-1", "a"), ("leader-1", "b"), ("leader-1", "c")]),
        "leader-1",
    )
    assert [(s.stage, s.members) for s in plan.stages] == [
        (0, ["a", "b", "c"]),
    ]
    assert plan.standby_members == ["d"]


def test_build_plan_after_a():
    plan = build_plan(
        _rel([("leader-1", "a"), ("a", "b"), ("a", "c")]),
        "leader-1",
    )
    assert [(s.stage, s.members) for s in plan.stages] == [
        (0, ["a"]),
        (1, ["b", "c"]),
    ]


def test_build_plan_join():
    plan = build_plan(
        _rel(
            [
                ("leader-1", "a"),
                ("leader-1", "b"),
                ("leader-1", "c"),
                ("a", "d"),
                ("b", "d"),
                ("c", "d"),
            ],
        ),
        "leader-1",
    )
    assert [(s.stage, s.members) for s in plan.stages] == [
        (0, ["a", "b", "c"]),
        (1, ["d"]),
    ]


def test_build_plan_chain():
    plan = build_plan(
        _rel([("leader-1", "a"), ("a", "b"), ("b", "c")]),
        "leader-1",
    )
    assert [(s.stage, s.members) for s in plan.stages] == [
        (0, ["a"]),
        (1, ["b"]),
        (2, ["c"]),
    ]


def test_build_plan_rejects_cycle():
    with pytest.raises(PlanError, match="cycle"):
        build_plan(
            _rel([("leader-1", "a"), ("a", "b"), ("b", "a")]),
            "leader-1",
        )


def test_build_plan_requires_leader_first():
    with pytest.raises(PlanError, match="first node"):
        build_plan(
            _rel([("a", "b"), ("b", "leader-1")]),
            "leader-1",
        )


def test_build_plan_rejects_leader_middle_target():
    with pytest.raises(PlanError, match="first stage"):
        build_plan(
            _rel([("leader-1", "b"), ("a", "b")]),
            "leader-1",
        )


def test_build_plan_leader_last():
    plan = build_plan(
        _rel([("leader-1", "a"), ("a", "b"), ("b", "leader-1")]),
        "leader-1",
    )
    assert plan.stages[-1].members == ["b"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_state_roundtrip(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    plan = build_plan(
        _rel([("leader-1", "a"), ("a", "b")]),
        "leader-1",
    )
    state = WorkflowState(
        team_id="t1",
        user_id="u1",
        plan=plan,
        stage_members=["a"],
        nodes={"a": WorkflowNodeState(), "b": WorkflowNodeState()},
        leader=WorkflowLeaderState(session_id="s1"),
    )
    _run(upsert_state(workflow_storage, state))
    got = _run(get_state(workflow_storage, "t1"))
    assert got is not None
    assert got.plan.stages[0].members == ["a"]
    assert got.nodes["a"].status == "pending"


def test_state_optimistic_lock(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    plan = build_plan(
        _rel([("leader-1", "a")], members=("a",)),
        "leader-1",
    )
    state = WorkflowState(
        team_id="t1",
        user_id="u1",
        plan=plan,
        stage_members=["a"],
        nodes={"a": WorkflowNodeState()},
        leader=WorkflowLeaderState(session_id="s1"),
    )
    _run(upsert_state(workflow_storage, state))
    first = _run(get_state(workflow_storage, "t1"))
    second = _run(get_state(workflow_storage, "t1"))
    assert first is not None and second is not None
    first.nodes["a"].status = "completed"
    assert _run(save_state(workflow_storage, first)) is True
    second.nodes["a"].status = "failed"
    assert _run(save_state(workflow_storage, second)) is False


def test_delete_state(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    plan = build_plan(
        _rel([("leader-1", "a")], members=("a",)),
        "leader-1",
    )
    state = WorkflowState(
        team_id="t1",
        user_id="u1",
        plan=plan,
        stage_members=["a"],
        nodes={"a": WorkflowNodeState()},
        leader=WorkflowLeaderState(session_id="s1"),
    )
    _run(upsert_state(workflow_storage, state))
    _run(delete_state(workflow_storage, "t1"))
    assert _run(get_state(workflow_storage, "t1")) is None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _seed_state(storage, edges, stage_members=("a",)):
    plan = build_plan(_rel(edges), "leader-1")
    state = WorkflowState(
        team_id="t1",
        user_id="u1",
        plan=plan,
        stage_members=list(stage_members),
        nodes={member: WorkflowNodeState() for member in plan.nodes},
        leader=WorkflowLeaderState(session_id="leader-session"),
    )
    _run(upsert_state(storage, state))
    return state


def test_submit_handoffs_leader(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    _seed_state(
        workflow_storage,
        [("leader-1", "a"), ("leader-1", "b"), ("leader-1", "c")],
        stage_members=("a", "b", "c"),
    )
    tool = SubmitHandoffs(
        storage=workflow_storage,
        user_id="u1",
        session_id="leader-session",
        agent_id="leader-1",
        team_id="t1",
        is_leader=True,
    )
    chunk = _run(
        tool(
            result="已拆解为三个子任务",
            handoffs=[
                {"target": "a", "task": "做 A"},
                {"target": "b", "task": "做 B"},
                {"target": "c", "task": "做 C"},
            ],
        ),
    )
    assert chunk.state.value == "success"
    state = _run(get_state(workflow_storage, "t1"))
    assert len(state.leader.handoffs) == 3


def test_submit_handoffs_worker_and_bad_target(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    _seed_state(workflow_storage, [("leader-1", "a"), ("a", "b")])
    state = _run(get_state(workflow_storage, "t1"))
    state.nodes["a"].status = "active"
    _run(save_state(workflow_storage, state))

    tool = SubmitHandoffs(
        storage=workflow_storage,
        user_id="u1",
        session_id="worker-a",
        agent_id="a",
        team_id="t1",
        is_leader=False,
    )
    bad = _run(
        tool(
            result="A 完成",
            handoffs=[{"target": "c", "task": "做 C"}],
        ),
    )
    assert bad.state.value == "error"

    good = _run(
        tool(
            result="A 完成",
            handoffs=[{"target": "b", "task": "做 B"}],
        ),
    )
    assert good.state.value == "success"
    state = _run(get_state(workflow_storage, "t1"))
    assert state.nodes["a"].status == "completed"


def test_submit_handoffs_rejects_long_result(workflow_storage):
    _run(team_store.ensure_team_tables(workflow_storage))
    _seed_state(workflow_storage, [("leader-1", "a")], stage_members=("a",))
    tool = SubmitHandoffs(
        storage=workflow_storage,
        user_id="u1",
        session_id="leader-session",
        agent_id="leader-1",
        team_id="t1",
        is_leader=True,
    )
    chunk = _run(
        tool(
            result="x" * (RESULT_MAX_CHARS + 1),
            handoffs=[{"target": "a", "task": "做 A"}],
        ),
    )
    assert chunk.state.value == "error"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_protocol_truncate():
    text = "中" * 200
    out, truncated = truncate_text(text, 50)
    assert truncated is True
    assert estimate_tokens(out) <= 60


def test_build_task_prompt_contains_inputs():
    prompt = build_task_prompt(
        "写摘要",
        [{"type": "file", "path": "user-data/outputs/a.md"}],
        role="研究员",
    )
    assert "写摘要" in prompt
    assert "user-data/outputs/a.md" in prompt
    assert "SubmitHandoffs" in prompt


# ---------------------------------------------------------------------------
# Toolkit replacement
# ---------------------------------------------------------------------------


def test_orchestrated_toolkit_replaces_routing_tools(workflow_storage):
    from types import SimpleNamespace

    from bocomadp.team_toolkit import _apply_orchestrated_toolkit

    _run(team_store.ensure_team_tables(workflow_storage))
    _run(
        team_store.upsert_team(
            workflow_storage,
            _rel([("leader-1", "a")], members=("a",)),
        ),
    )
    _seed_state(workflow_storage, [("leader-1", "a")], stage_members=("a",))
    team = SimpleNamespace(
        id="t1",
        session_id="leader-session",
    )
    async def get_session(user_id, agent_id, session_id):
        if session_id == "leader-session":
            return SimpleNamespace(
                id="leader-session",
                agent_id="leader-1",
                team_id="t1",
            )
        return None

    workflow_storage.get_session = get_session

    class _Toolkit:
        def __init__(self):
            self.tool_groups = [
                SimpleNamespace(
                    name="basic",
                    tools=[
                        SimpleNamespace(name="TeamSay"),
                        SimpleNamespace(name="Bash"),
                    ],
                ),
            ]
            self.added = []

        async def add_tool(self, tool, group_name="basic"):
            self.added.append(tool)

    toolkit = _Toolkit()
    applied = _run(
        _apply_orchestrated_toolkit(
            toolkit,
            storage=workflow_storage,
            user_id="u1",
            agent_record=SimpleNamespace(id="leader-1"),
            session_record=SimpleNamespace(id="leader-session"),
            team=team,
            team_role="leader",
        ),
    )
    assert applied is True
    names = [tool.name for tool in toolkit.tool_groups[0].tools]
    assert "TeamSay" not in names
    assert "Bash" in names
    assert any(tool.name == "SubmitHandoffs" for tool in toolkit.added)


# ---------------------------------------------------------------------------
# Orchestrator (plan -> dispatch -> final)
# ---------------------------------------------------------------------------


def test_orchestrator_dispatches_and_finalizes(workflow_storage, monkeypatch):
    from bocomadp.team import orchestrator as orchestrator_mod
    from bocomadp.team.orchestrator import WorkflowOrchestrator

    _run(team_store.ensure_team_tables(workflow_storage))
    rel = _rel(
        [("leader-1", "a"), ("leader-1", "b")],
        members=("a", "b"),
    )
    _run(team_store.upsert_team(workflow_storage, rel))

    leader_session = SimpleNamespace(
        id="leader-session",
        agent_id="leader-1",
        team_id=None,
        config=SimpleNamespace(
            workspace_id="ws-1",
            chat_model_config=None,
            fallback_chat_model_config=None,
        ),
    )
    workflow_storage.sessions = {"leader-session": leader_session}
    workflow_storage.runtime_teams = {}

    async def get_session(user_id, agent_id, session_id):
        return workflow_storage.sessions.get(session_id)

    async def get_agent(user_id, agent_id):
        return SimpleNamespace(data=SimpleNamespace(name=agent_id))

    async def list_sessions(user_id, agent_id):
        return [
            session
            for session in workflow_storage.sessions.values()
            if getattr(session, "agent_id", None) == agent_id
        ]

    async def upsert_session(user_id, agent_id, config, **kwargs):
        session = SimpleNamespace(
            id=f"session-{agent_id}",
            agent_id=agent_id,
            team_id=None,
            config=config,
        )
        workflow_storage.sessions[session.id] = session
        return session

    async def set_session_team_id(user_id, session_id, team_id):
        session = workflow_storage.sessions.get(session_id)
        if session is not None:
            session.team_id = team_id

    async def upsert_team(user_id, team):
        workflow_storage.runtime_teams[team.id] = team

    async def get_team(user_id, team_id):
        return workflow_storage.runtime_teams.get(team_id)

    workflow_storage.get_session = get_session
    workflow_storage.get_agent = get_agent
    workflow_storage.list_sessions = list_sessions
    workflow_storage.upsert_session = upsert_session
    workflow_storage.set_session_team_id = set_session_team_id
    workflow_storage.upsert_team = upsert_team
    workflow_storage.get_team = get_team

    deltas = []

    from contextlib import asynccontextmanager

    class _Bus:
        async def queue_push(self, key, payload):
            deltas.append(("push", key, payload))

        @asynccontextmanager
        async def acquire_lock(self, key, ttl_secs=300):
            yield

    async def fake_enqueue(bus, **kwargs):
        deltas.append(("wake", kwargs))

    monkeypatch.setattr(
        orchestrator_mod,
        "enqueue_run_trigger",
        fake_enqueue,
    )
    orchestrator = WorkflowOrchestrator(
        storage=workflow_storage,
        message_bus=_Bus(),
    )

    state = _run(
        orchestrator.ensure_plan(
            user_id="u1",
            leader_agent_id="leader-1",
            session_id="leader-session",
        ),
    )
    assert state is not None
    assert state.plan.stages[0].members == ["a", "b"]

    state.leader.handoffs = [
        SimpleNamespace(target="a", task="做 A", inputs=[]),
        SimpleNamespace(target="b", task="做 B", inputs=[]),
    ]
    _run(orchestrator.dispatch_stage(state, 0))
    worker_sessions = {
        key for key in workflow_storage.sessions if key.startswith("session-")
    }
    assert worker_sessions == {"session-a", "session-b"}
    assert len([d for d in deltas if d[0] == "wake"]) == 2

    state = _run(get_state(workflow_storage, state.team_id))
    for member in ("a", "b"):
        state.nodes[member].status = "completed"
        state.nodes[member].result = SimpleNamespace(summary=f"{member} ok", path=None)
    _run(orchestrator._check_stage_done(state))
    leader_wakes = [
        d for d in deltas
        if d[0] == "wake" and d[1]["session_id"] == "leader-session"
    ]
    assert len(leader_wakes) == 1

    # A stale caller (all workers completed, phase still "first") must not
    # enqueue a second final hint after the first one has already won.
    stale = state.model_copy(deep=True)
    stale.leader.phase = "first"
    stale.status = "running"
    _run(orchestrator._check_stage_done(stale))
    leader_wakes = [
        d for d in deltas
        if d[0] == "wake" and d[1]["session_id"] == "leader-session"
    ]
    assert len(leader_wakes) == 1


# ---------------------------------------------------------------------------
# Shared workspace
# ---------------------------------------------------------------------------


def test_workspace_worker_binds_to_leader():
    class _Inner:
        def __init__(self):
            self.calls = []

        async def get_workspace(self, user_id, agent_id, session_id, workspace_id=None):
            self.calls.append((user_id, agent_id, session_id, workspace_id))
            return ("workspace", agent_id, session_id)

    storage = SimpleNamespace(
        sessions={
            "worker-1": SimpleNamespace(
                id="worker-1",
                team_id="t1",
            ),
        },
        teams={
            "t1": SimpleNamespace(
                id="t1",
                session_id="leader-session",
            ),
        },
        leader=SimpleNamespace(
            id="leader-session",
            agent_id="leader-1",
            config=SimpleNamespace(workspace_id="ws-1"),
        ),
    )

    async def get_session(user_id, agent_id, session_id):
        if session_id == "leader-session":
            return storage.leader
        return storage.sessions.get(session_id)

    async def get_team(user_id, team_id):
        return storage.teams.get(team_id)

    storage.get_session = get_session
    storage.get_team = get_team
    inner = _Inner()
    manager = TeamWorkspaceManager(inner, storage)
    result = _run(manager.get_workspace("u1", "worker-agent", "worker-1"))
    assert result == ("workspace", "leader-1", "leader-session")
    assert inner.calls == [("u1", "leader-1", "leader-session", "ws-1")]


def test_workspace_without_team_uses_own():
    class _Inner:
        def __init__(self):
            self.calls = []

        async def get_workspace(self, user_id, agent_id, session_id, workspace_id=None):
            self.calls.append((user_id, agent_id, session_id, workspace_id))
            return ("own", agent_id, session_id)

    storage = SimpleNamespace(
        sessions={
            "plain": SimpleNamespace(id="plain", team_id=None),
        },
    )

    async def get_session(user_id, agent_id, session_id):
        return storage.sessions.get(session_id)

    async def get_team(user_id, team_id):
        raise AssertionError("team lookup must not happen without team_id")

    storage.get_session = get_session
    storage.get_team = get_team
    inner = _Inner()
    manager = TeamWorkspaceManager(inner, storage)
    result = _run(manager.get_workspace("u1", "plain-agent", "plain", "ws-own"))
    assert result == ("own", "plain-agent", "plain")
    assert inner.calls == [("u1", "plain-agent", "plain", "ws-own")]
