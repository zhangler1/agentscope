# -*- coding: utf-8 -*-
"""GET /agent/ 路由接线集成测试。

背景（为什么需要这个文件）：
- 专家团 18 个用例全是纯单元测试，monkeypatch 把框架函数换成了
  "签名更宽松的替身"，导致 agent.py list_agents 里把 parent_agent_id
  硬塞给框架 list_resource 的接线错误没被任何测试抓住。
- 本文件走真实 HTTP（TestClient），用真 SQLite storage + 真访问层，
  专门锁住 GET /agent/ 的两种模式：
    1. 不带 parent_agent_id  → 顶层列表干净（团队成员被藏起来）
    2. 带 parent_agent_id    → 只返回该团队名册里的成员
- 只要再有人动这条接线（把团队参数塞回框架 / 漏传依赖），
  一跑本文件就红。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import team_store
from bocomadp.routers.agent import agent_router
# 框架内置 agent_router 只用于"摘除"（与 main.py 装配一致：专家团能力
# 由 bocomadp 版 agent_router 覆盖）。
from agentscope.app._router._agent import (
    agent_router as _framework_agent_router,
)

HEADERS = {"X-User-ID": "test-user"}


def _remove_framework_agent_routes(app) -> None:
    """摘除框架内置 /agent 路由（main.py 同款逻辑），避免其抢占 /agent/ CRUD。

    FastAPI 0.141+ include_router 用 _IncludedRouter 懒包装（持有
    original_router），须按引用身份判断；旧版则按路径判断。
    """
    fw_paths = {
        r.path
        for r in _framework_agent_router.routes
        if getattr(r, "path", "").startswith("/agent")
    }

    def _is_fw(r) -> bool:
        original = getattr(r, "original_router", None)
        if original is not None:
            return original is _framework_agent_router
        return getattr(r, "path", "") in fw_paths

    app.router.routes[:] = [r for r in app.router.routes if not _is_fw(r)]


@pytest.fixture
def client(tmp_path):
    """完整 app（sqlite 存储 + 团队关系表）+ bocomadp agent_router 装配。"""
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'mem_main.db'}",
        create_tables=True,
    )
    # 专家团关系表是 bocomadp 独立 metadata，需显式建表；
    # engine 在 storage 进入上下文时才创建，必须先 async with。
    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)

    asyncio.run(_provision())
    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    # 与 main.py 装配一致：先摘除框架内置 /agent 路由，再挂 bocomadp
    # agent_router（团队端点 /agent/{id}/team/*、/agent/schema/v2 等 +
    # /agent/ CRUD），避免框架路由抢占。
    _remove_framework_agent_routes(app)
    app.include_router(agent_router)
    with TestClient(app) as test_client:
        yield test_client


def _create_leader(client) -> str:
    resp = client.post(
        "/agent/",
        json={"name": "leader"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["agent_id"]


def _create_member(client, leader_id: str, name: str = "member") -> str:
    resp = client.post(
        "/agent/",
        json={"name": name, "parent_agent_id": leader_id},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return resp.json()["agent_id"]


class TestListTopLevel:
    """模式 1：不带 parent_agent_id 的顶层列表。"""

    def test_top_level_hides_team_members(self, client):
        leader_id = _create_leader(client)
        member_id = _create_member(client, leader_id)

        resp = client.get("/agent/", headers=HEADERS)
        assert resp.status_code == 200
        ids = {a["id"] for a in resp.json()["agents"]}

        # 领导（无父）出现在顶层列表
        assert leader_id in ids
        # 团队成员被藏起来——顶层是"个人工作台"，不是"团队名册"
        assert member_id not in ids

    def test_top_level_empty_without_agents(self, client):
        resp = client.get("/agent/", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["agents"] == []
        assert resp.json()["total"] == 0


class TestListWithParent:
    """模式 2：带 parent_agent_id 的团队名册过滤。"""

    def test_with_parent_returns_only_members(self, client):
        leader_id = _create_leader(client)
        member_a = _create_member(client, leader_id, name="member-a")
        member_b = _create_member(client, leader_id, name="member-b")
        # 另一个团队（隔离验证：不混入名册）
        other_leader = _create_leader(client)
        other_member_id = _create_member(
            client, other_leader, name="other-member"
        )

        resp = client.get(
            f"/agent/?parent_agent_id={leader_id}",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        ids = {a["id"] for a in resp.json()["agents"]}

        # 本团队两名成员都在名册里
        assert member_a in ids
        assert member_b in ids
        # leader 自己是"领导"不是"成员"，不出现在自己的名册里
        assert leader_id not in ids
        # 别的团队的成员不混进来（拿真实 id 断言，不能拿 name 当 id）
        assert other_member_id not in ids

    def test_with_parent_unknown_leader_returns_empty(self, client):
        resp = client.get(
            "/agent/?parent_agent_id=no-such-leader",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["agents"] == []


class TestPagination:
    """分页参数 pageNum / pageSize 的行为锁死。

    约定（docs/api.md 第三节）：
    - 不传分页参数 → 默认第 1 页、每页 5 条；
    - pageNum 从 1 开始（>=1），pageSize 范围 1~100（越界 422）；
    - total 恒为分页前的完整总数（与当前页 agents 数量可不同）。
    """

    def _create_agents(self, client, n: int) -> list[str]:
        """建 n 个顶层可见的 agent（leader），返回 id 列表。"""
        return [_create_leader(client) for _ in range(n)]

    def test_default_returns_first_page_of_five(self, client):
        ids = self._create_agents(client, 7)

        resp = client.get("/agent/", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        page_ids = {a["id"] for a in body["agents"]}
        # 默认每页 5 条，7 个 agent 只有 5 个在第一页
        assert len(page_ids) == 5
        assert page_ids.issubset(set(ids))
        # total 是分页前的完整总数
        assert body["total"] == 7

    def test_second_page_returns_remaining(self, client):
        ids = self._create_agents(client, 7)

        resp = client.get("/agent/?pageNum=2&pageSize=5", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        page_ids = {a["id"] for a in body["agents"]}
        # 第 2 页只有剩下的 2 个
        assert len(page_ids) == 2
        assert page_ids.issubset(set(ids))
        assert body["total"] == 7

        # 两页拼起来正好是全集（无重复、无遗漏）
        page1 = client.get("/agent/?pageNum=1&pageSize=5", headers=HEADERS)
        all_ids = {a["id"] for a in page1.json()["agents"]} | page_ids
        assert all_ids == set(ids)
        assert len(all_ids) == 7

    def test_pages_do_not_overlap(self, client):
        self._create_agents(client, 7)

        page1 = client.get("/agent/?pageNum=1&pageSize=5", headers=HEADERS).json()
        page2 = client.get("/agent/?pageNum=2&pageSize=5", headers=HEADERS).json()
        page1_ids = {a["id"] for a in page1["agents"]}
        page2_ids = {a["id"] for a in page2["agents"]}
        # 分页切片不允许同一 agent 出现在两页
        assert page1_ids.isdisjoint(page2_ids)
        assert page1["total"] == page2["total"] == 7

    def test_beyond_last_page_returns_empty_but_total_stays(self, client):
        self._create_agents(client, 7)

        resp = client.get("/agent/?pageNum=99&pageSize=5", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["agents"] == []
        # 越界页 agents 为空，但 total 仍是完整总数
        assert body["total"] == 7

    def test_custom_page_size(self, client):
        ids = self._create_agents(client, 7)

        resp = client.get("/agent/?pageNum=1&pageSize=3", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["agents"]) == 3
        assert {a["id"] for a in body["agents"]}.issubset(set(ids))
        assert body["total"] == 7

    def test_page_size_one(self, client):
        ids = self._create_agents(client, 3)

        resp = client.get("/agent/?pageNum=3&pageSize=1", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["agents"]) == 1
        assert {a["id"] for a in body["agents"]}.issubset(set(ids))
        assert body["total"] == 3

    def test_invalid_page_num_rejected(self, client):
        # pageNum 从 1 开始，0 非法 → 422
        resp = client.get("/agent/?pageNum=0&pageSize=5", headers=HEADERS)
        assert resp.status_code == 422

    def test_invalid_page_size_rejected(self, client):
        # pageSize 范围 1~100，0 和 101 都非法 → 422
        assert client.get("/agent/?pageNum=1&pageSize=0", headers=HEADERS).status_code == 422
        assert (
            client.get("/agent/?pageNum=1&pageSize=101", headers=HEADERS).status_code
            == 422
        )


class TestSelfBuiltMemberInvitable:
    """自建成员的 invitable 开关（运行时团长 AgentInvite 借调的前置条件）。

    邀请功能已移除（专家团成员只能由 parent_agent_id 自建、独属于本团），
    但自建成员创建时仍自动 invitable=true 且描述非空，否则团长运行时
    无法用 AgentInvite 拉起该成员。这里锁死该自动行为。
    """

    def test_self_built_member_auto_invitable(self, client):
        """自建成员创建后自动 invitable=true 且描述非空（团长可借调）。"""
        leader_id = _create_leader(client)
        member_id = _create_member(client, leader_id, name="self-child")

        resp = client.get(
            f"/agent/?parent_agent_id={leader_id}",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        members = {a["id"]: a for a in resp.json()["agents"]}
        data = members[member_id]["data"]
        assert data["invite_config"]["invitable"] is True
        assert data["invite_config"]["invite_description"].strip()
