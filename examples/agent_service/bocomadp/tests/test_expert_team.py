# -*- coding: utf-8 -*-
"""专家团（expert team）核心模块测试：迁移进 bocomadp 的团队关系与协作逻辑。

覆盖（全部是从 src 迁到 bocomadp 的专家团功能）：
- team_store             团队档案模型 + 异步访问层（SQLite 临时库替换 engine）
- session_team_cascade   删除 agent 时的团队级联策略（自建级联删 / 外邀只摘链）
- team_toolkit           _allowed_handoff_targets：workflow 严格交接白名单解析
- team_briefing          leader 系统提示词简报拼接
- agent_list_sort        资源列表按 updated_at 倒序
- toolkit_whitelist      工具白名单过滤（_keep 纯函数）

pytest-asyncio 未安装，异步用例统一用 ``asyncio.run()`` 包裹
（与 test_memory_config.py 一致）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bocomadp import agent_list_sort, session_team_cascade, team_store
from bocomadp import team_briefing, team_toolkit, toolkit_whitelist
from bocomadp.team_store import ExpertTeamMember, ExpertTeamRelation, HandoffRelation


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1) team_store 业务模型
# ---------------------------------------------------------------------------


def test_relation_member_ids_and_relation_of():
    rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        members=[
            ExpertTeamMember(agent_id="child-1", relation="self_built"),
            ExpertTeamMember(agent_id="guest-1", relation="invited"),
        ],
    )
    assert rel.member_ids == ["child-1", "guest-1"]
    assert rel.relation_of("child-1") == "self_built"
    assert rel.relation_of("guest-1") == "invited"
    assert rel.relation_of("nobody") is None
    assert rel.is_self_built("child-1") is True
    assert rel.is_self_built("guest-1") is False


def test_add_member_idempotent():
    rel = ExpertTeamRelation(user_id="u1", leader_agent_id="l1")
    rel.add_member("a", "self_built")
    rel.add_member("a", "invited")  # 同 id 更新标记，不重复
    assert len(rel.members) == 1
    assert rel.relation_of("a") == "invited"


def test_remove_member():
    rel = ExpertTeamRelation(user_id="u1", leader_agent_id="l1")
    rel.add_member("a", "self_built")
    assert rel.remove_member("a") is True
    assert rel.remove_member("a") is False
    assert rel.member_ids == []


# ---------------------------------------------------------------------------
# 2) team_store 异步访问层（SQLite 临时库替换 engine）
# ---------------------------------------------------------------------------


@pytest.fixture
def team_storage(tmp_path):
    """fake storage：_engine + _session_factory 指向临时 sqlite 文件库。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'team.db'}")

    class _FakeStorage:
        pass

    storage = _FakeStorage()
    storage._engine = engine
    storage._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return storage


def test_upsert_get_roundtrip(team_storage):
    _run(team_store.ensure_team_tables(team_storage))
    rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="workflow",
        members=[ExpertTeamMember(agent_id="child-1", relation="self_built")],
        handoff_relations=[
            HandoffRelation(
                from_agent_id="leader-1",
                to_agent_id="child-1",
                description="写文案",
            ),
        ],
        max_members=5,
    )
    _run(team_store.upsert_team(team_storage, rel))

    got = _run(team_store.get_team(team_storage, "u1", "leader-1"))
    assert got is not None
    assert got.user_id == "u1"
    assert got.leader_agent_id == "leader-1"
    assert got.collaboration_mode == "workflow"
    assert got.member_ids == ["child-1"]
    assert got.handoff_relations[0].to_agent_id == "child-1"
    assert got.max_members == 5


def test_get_missing_returns_none(team_storage):
    _run(team_store.ensure_team_tables(team_storage))
    assert _run(team_store.get_team(team_storage, "u1", "nope")) is None


def test_upsert_overwrites(team_storage):
    _run(team_store.ensure_team_tables(team_storage))
    _run(
        team_store.upsert_team(
            team_storage,
            ExpertTeamRelation(
                user_id="u1",
                leader_agent_id="l1",
                collaboration_mode="free_handoff",
            ),
        ),
    )
    _run(
        team_store.upsert_team(
            team_storage,
            ExpertTeamRelation(
                user_id="u1",
                leader_agent_id="l1",
                collaboration_mode="workflow",
            ),
        ),
    )
    got = _run(team_store.get_team(team_storage, "u1", "l1"))
    assert got is not None and got.collaboration_mode == "workflow"


def test_list_teams_filters_by_user(team_storage):
    _run(team_store.ensure_team_tables(team_storage))
    _run(
        team_store.upsert_team(
            team_storage,
            ExpertTeamRelation(user_id="u1", leader_agent_id="l1"),
        ),
    )
    _run(
        team_store.upsert_team(
            team_storage,
            ExpertTeamRelation(user_id="u2", leader_agent_id="l2"),
        ),
    )
    teams = _run(team_store.list_teams(team_storage, "u1"))
    assert [t.leader_agent_id for t in teams] == ["l1"]


def test_delete_team(team_storage):
    _run(team_store.ensure_team_tables(team_storage))
    _run(
        team_store.upsert_team(
            team_storage,
            ExpertTeamRelation(user_id="u1", leader_agent_id="l1"),
        ),
    )
    _run(team_store.delete_team(team_storage, "u1", "l1"))
    assert _run(team_store.get_team(team_storage, "u1", "l1")) is None


# ---------------------------------------------------------------------------
# 3) session_team_cascade：删除 agent 时的团队级联策略
# ---------------------------------------------------------------------------


def test_cascade_deletes_self_built_members_then_team(monkeypatch):
    rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        members=[
            ExpertTeamMember(agent_id="child-1", relation="self_built"),
            ExpertTeamMember(agent_id="guest-1", relation="invited"),
        ],
    )

    async def fake_get_team(storage, user_id, agent_id):
        return rel if agent_id == "leader-1" else None

    async def fake_list_teams(storage, user_id):
        return [rel]

    removed_teams: list[str] = []

    async def fake_delete_team(storage, user_id, leader_agent_id):
        removed_teams.append(leader_agent_id)

    monkeypatch.setattr(team_store, "get_team", fake_get_team)
    monkeypatch.setattr(team_store, "list_teams", fake_list_teams)
    monkeypatch.setattr(team_store, "delete_team", fake_delete_team)

    deleted: list[str] = []

    class _FakeService:
        _storage = object()

        async def delete_agent(self, user_id, agent_id):
            deleted.append(agent_id)
            return True

    async def fake_original(self, user_id, agent_id):
        deleted.append(agent_id)
        return True

    monkeypatch.setattr(session_team_cascade, "_original_delete_agent", fake_original)

    svc = _FakeService()
    result = _run(session_team_cascade._delete_agent_with_cascade(svc, "u1", "leader-1"))
    assert result is True
    # 自建成员被级联删、外邀成员不删、团队行被解散
    assert deleted == ["child-1", "leader-1"]
    assert removed_teams == ["leader-1"]


def test_cascade_detaches_invited_member_from_other_team(monkeypatch):
    leader_rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        members=[
            ExpertTeamMember(agent_id="guest-1", relation="invited"),
        ],
        handoff_relations=[
            HandoffRelation(
                from_agent_id="leader-1",
                to_agent_id="guest-1",
                description="翻译",
            ),
        ],
    )

    async def fake_get_team(storage, user_id, agent_id):
        return None  # guest-1 不是任何 leader

    async def fake_list_teams(storage, user_id):
        return [leader_rel]

    upserted: list[ExpertTeamRelation] = []

    async def fake_upsert_team(storage, rel):
        upserted.append(rel)

    monkeypatch.setattr(team_store, "get_team", fake_get_team)
    monkeypatch.setattr(team_store, "list_teams", fake_list_teams)
    monkeypatch.setattr(team_store, "upsert_team", fake_upsert_team)

    deleted: list[str] = []

    class _FakeService:
        _storage = object()

        async def delete_agent(self, user_id, agent_id):
            deleted.append(agent_id)
            return True

    async def fake_original(self, user_id, agent_id):
        deleted.append(agent_id)
        return True

    monkeypatch.setattr(session_team_cascade, "_original_delete_agent", fake_original)

    svc = _FakeService()
    _run(session_team_cascade._delete_agent_with_cascade(svc, "u1", "guest-1"))
    # 外邀成员本身照删，leader 名册里只剩"摘链"动作
    assert deleted == ["guest-1"]
    assert leader_rel.member_ids == []
    assert leader_rel.handoff_relations == []
    assert len(upserted) == 1


# ---------------------------------------------------------------------------
# 4) team_toolkit._allowed_handoff_targets：workflow 严格交接白名单
# ---------------------------------------------------------------------------


def _mk_storage(team, leader_session):
    """构造 fake storage：get_team 直接返回团队、get_session 按 id 匹配。"""

    class _Storage:
        async def get_team(self, user_id, team_id):
            # 注意：team_id 是团队 ID；team.session_id 是 leader 的会话 ID，
            # 两者不同义，这里按"查到团队就返回"打桩。
            return team

        async def get_session(self, user_id, agent_id, session_id):
            if leader_session is not None and leader_session.id == session_id:
                return leader_session
            return None

    return _Storage()


def _patch_get_team(monkeypatch, rel):
    """把 team_store.get_team 换成返回固定团队的 async 桩。"""

    async def fake_get_team(storage, user_id, leader_agent_id):
        return rel

    monkeypatch.setattr(team_store, "get_team", fake_get_team)


def test_worker_workflow_allowed_only_leader(monkeypatch):
    team = SimpleNamespace(session_id="leader-session")
    leader_session = SimpleNamespace(id="leader-session", agent_id="leader-1")
    workflow_rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="workflow",
        members=[
            ExpertTeamMember(agent_id="child-1", relation="self_built"),
            ExpertTeamMember(agent_id="child-2", relation="self_built"),
        ],
    )
    _patch_get_team(monkeypatch, workflow_rel)

    storage = _mk_storage(team, leader_session)
    agent_record = SimpleNamespace(id="child-1")
    worker_session = SimpleNamespace(id="worker-session", team_id="team-1")

    _team, role, allowed = _run(
        team_toolkit._allowed_handoff_targets(storage, "u1", agent_record, worker_session),
    )
    assert role == "worker"
    assert allowed == {"leader-1"}


def test_leader_workflow_allowed_to_endpoints(monkeypatch):
    team = SimpleNamespace(session_id="leader-session")
    leader_session = SimpleNamespace(id="leader-session", agent_id="leader-1")
    workflow_rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="workflow",
        handoff_relations=[
            HandoffRelation(from_agent_id="leader-1", to_agent_id="child-1"),
            HandoffRelation(from_agent_id="leader-1", to_agent_id="child-2"),
        ],
    )
    _patch_get_team(monkeypatch, workflow_rel)

    storage = _mk_storage(team, leader_session)
    leader_record = SimpleNamespace(id="leader-1")
    leader_session_rec = SimpleNamespace(id="leader-session", team_id="team-1")

    _team, role, allowed = _run(
        team_toolkit._allowed_handoff_targets(
            storage,
            "u1",
            leader_record,
            leader_session_rec,
        ),
    )
    assert role == "leader"
    assert allowed == {"child-1", "child-2"}


def test_free_handoff_no_restriction(monkeypatch):
    team = SimpleNamespace(session_id="leader-session")
    leader_session = SimpleNamespace(id="leader-session", agent_id="leader-1")
    free_rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="free_handoff",
        handoff_relations=[
            HandoffRelation(from_agent_id="leader-1", to_agent_id="child-1"),
        ],
    )
    _patch_get_team(monkeypatch, free_rel)

    storage = _mk_storage(team, leader_session)
    leader_record = SimpleNamespace(id="leader-1")
    leader_session_rec = SimpleNamespace(id="leader-session", team_id="team-1")

    _team, role, allowed = _run(
        team_toolkit._allowed_handoff_targets(
            storage,
            "u1",
            leader_record,
            leader_session_rec,
        ),
    )
    assert role == "leader"
    assert allowed is None  # free_handoff 不设白名单


def test_non_team_session_returns_none(monkeypatch):
    _patch_get_team(monkeypatch, None)

    storage = _mk_storage(None, None)
    agent_record = SimpleNamespace(id="plain-1")
    plain_session = SimpleNamespace(id="plain-session", team_id=None)

    team, role, allowed = _run(
        team_toolkit._allowed_handoff_targets(storage, "u1", agent_record, plain_session),
    )
    assert team is None
    assert role is None
    assert allowed is None


# ---------------------------------------------------------------------------
# 5) team_briefing：leader 系统提示词简报
# ---------------------------------------------------------------------------


def _mk_agent(agent_id, name, desc):
    return SimpleNamespace(
        id=agent_id,
        data=SimpleNamespace(
            name=name,
            invite_config=SimpleNamespace(invite_description=desc),
        ),
    )


def test_briefing_appends_members_and_handoffs(monkeypatch):
    from agentscope.app._tool._constants import HANDLE_LEN

    rel = ExpertTeamRelation(
        user_id="u1",
        leader_agent_id="leader-1",
        collaboration_mode="workflow",
        members=[ExpertTeamMember(agent_id="child-1", relation="self_built")],
        handoff_relations=[
            HandoffRelation(
                from_agent_id="leader-1",
                to_agent_id="child-1",
                description="写文案",
            ),
        ],
    )
    _patch_get_team(monkeypatch, rel)

    class _Storage:
        async def get_agent(self, owner_id, agent_id):
            table = {
                "child-1": _mk_agent("child-1", "文案助手", "负责写文案"),
            }
            return table.get(agent_id)

    record = SimpleNamespace(
        user_id="u1",
        id="leader-1",
        data=SimpleNamespace(system_prompt="你是老板。"),
    )
    prompt = _run(team_briefing._build_leader_system_prompt(record, _Storage()))
    assert prompt.startswith("你是老板。")
    assert "# Expert team briefing" in prompt
    assert f"- 文案助手@{'child-1'[:HANDLE_LEN]}: 负责写文案" in prompt
    assert "## Collaboration / handoff order" in prompt
    assert "Mode: **workflow**" in prompt


def test_briefing_plain_agent_unchanged(monkeypatch):
    _patch_get_team(monkeypatch, None)

    record = SimpleNamespace(
        user_id="u1",
        id="plain-1",
        data=SimpleNamespace(system_prompt="普通 agent 提示词"),
    )
    prompt = _run(team_briefing._build_leader_system_prompt(record, object()))
    assert prompt == "普通 agent 提示词"


# ---------------------------------------------------------------------------
# 6) agent_list_sort / toolkit_whitelist 纯逻辑
# ---------------------------------------------------------------------------


def test_sorted_list_resource_recent_first(monkeypatch):
    views = [
        SimpleNamespace(updated_at=datetime(2026, 8, 18, 9, 0)),
        SimpleNamespace(updated_at=datetime(2026, 8, 19, 9, 0)),
        SimpleNamespace(updated_at=datetime(2026, 8, 17, 9, 0)),
    ]

    async def fake_original(self, viewer_id, kind, parent_agent_id=None):
        return list(views)

    monkeypatch.setattr(agent_list_sort, "_original_list_resource", fake_original)
    out = _run(agent_list_sort._sorted_list_resource(object(), "u1", None))
    assert [v.updated_at for v in out] == [
        datetime(2026, 8, 19, 9, 0),
        datetime(2026, 8, 18, 9, 0),
        datetime(2026, 8, 17, 9, 0),
    ]


def test_keep_filters_by_name():
    tool = SimpleNamespace(name="get_current_time")
    assert toolkit_whitelist._keep(tool, {"get_current_time"}) is True
    assert toolkit_whitelist._keep(tool, {"other_tool"}) is False
