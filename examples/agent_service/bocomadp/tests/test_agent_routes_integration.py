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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import memory_config, team_store
from bocomadp.routers.agent import agent_router
from bocomadp.routers.agent_api import install_agent_memory_router

HEADERS = {"X-User-ID": "test-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """完整 app（sqlite 存储 + 侧边记忆表 + 团队关系表）+ 包裹路由接管。"""
    side_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mem_side.db'}",
    )

    async def _init_side():
        async with side_engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS agent_memory_configs ("
                    "user_id VARCHAR(255) NOT NULL, "
                    "agent_id VARCHAR(255) NOT NULL, "
                    "memory_update_prompt TEXT NOT NULL DEFAULT '', "
                    "memory_enabled BOOLEAN NOT NULL DEFAULT FALSE, "
                    "memory_type INTEGER NOT NULL DEFAULT 0, "
                    "memory_update_rounds INTEGER NOT NULL DEFAULT 10, "
                    "updated_at DATETIME NOT NULL, "
                    "PRIMARY KEY (user_id, agent_id)"
                    ")",
                ),
            )

    asyncio.run(_init_side())

    async def fake_engine():
        return side_engine

    monkeypatch.setattr(memory_config, "_get_engine", fake_engine)

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
    # 与 main.py 装配一致：先挂 bocomadp agent_router（团队端点
    # /agent/{id}/team/*、/agent/schema/v2 等），再由记忆包裹路由接管
    # /agent/ 的 4 条 CRUD。顺序不能反，否则团队端点 404。
    app.include_router(agent_router)
    install_agent_memory_router(app)
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

    def test_top_level_keeps_invited_members(self, client):
        """邀请成员（invited）不是自建成员，顶层列表必须可见。

        区分两种成员身份：
        - parent_agent_id 创建 → self_built → 顶层隐藏
        - 普通 agent 被邀请进团队 → invited → 顶层可见
        曾踩过坑：agent.py 顶层过滤用 team.members 把 invited 也藏了，
        违反"子智能体不展示只对自建成员成立"（回归）。本用例锁死。
        """
        leader_id = _create_leader(client)
        invited_id = _create_member(client, leader_id, name="invited-plain")
        # 不挂 parent：这个普通 agent 本身在顶层可见
        plain = client.post(
            "/agent/",
            json={"name": "plain-invitee"},
            headers=HEADERS,
        )
        assert plain.status_code == 201
        plain_id = plain.json()["agent_id"]

        # 把普通 agent 邀请进团队（变成 invited 成员）
        resp = client.post(
            f"/agent/{leader_id}/team/members",
            json={"agent_id": plain_id},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        cfg = resp.json()
        member_ids = {m["agent_id"] for m in cfg["members"]}
        assert member_ids == {invited_id, plain_id}
        # 身份区分：parent 创建的是 self_built，邀请进来的是 invited
        by_id = {m["agent_id"]: m["is_self_built"] for m in cfg["members"]}
        assert by_id[invited_id] is True
        assert by_id[plain_id] is False

        top = client.get("/agent/", headers=HEADERS)
        assert top.status_code == 200
        ids = {a["id"] for a in top.json()["agents"]}
        # leader + 邀请成员（invited）都在顶层
        assert leader_id in ids
        assert plain_id in ids
        # 只有自建成员被藏
        assert invited_id not in ids

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
