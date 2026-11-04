# -*- coding: utf-8 -*-
"""角色（role）：用户隔离 + 审批权限改造集成测试。

覆盖（走真实 HTTP / 独立 SQLite storage + 独立 SQLite 角色库）：

1. GET /agent/market/my-role —— 角色自检：
   - reviewer 角色用户返回 ["reviewer"]；普通用户返回 []；
2. GET /agent/ —— 列表隔离 vs 管理员全量：
   - 普通用户只看到自己创建的；
   - reviewer 角色用户看到全部（含他人 + 自己的）；
3. 审批接口权限门禁：
   - 普通用户 GET /reviews → 403；reviewer 角色用户 GET /reviews → 200；
4. reviewers 管理接口（复用既有 POST/PUT/DELETE）+ 末位保护：
   - 非名单内写操作 403；名单内新增幂等；删单条 204 / 不在名单 404；
   - 删最后一人 / PUT 清空 → 409；
5. role_store 底层：has_role / assign_role / revoke_role / list_my_roles /
   overwrite_role_members（末位保护 empty）。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与
test_agent_market.py 一致）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import create_app
from agentscope.app._router._agent import agent_router as _framework_agent_router
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AgentData, AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import market_review_store, market_store, role_store, team_store
from bocomadp.routers.agent import agent_router
from bocomadp.routers.market import market_router

HDR_USER = {"X-User-ID": "test-user"}
HDR_ALICE = {"X-User-ID": "alice"}
HDR_REVIEWER = {"X-User-ID": "reviewer-1"}


def _run(coro):
    return asyncio.run(coro)


def _remove_framework_agent_routes(app) -> None:
    fw_paths = {
        r.path for r in _framework_agent_router.routes
        if getattr(r, "path", "").startswith("/agent")
    }

    def _is_fw(r):
        original = getattr(r, "original_router", None)
        if original is not None:
            return original is _framework_agent_router
        return getattr(r, "path", "") in fw_paths

    app.router.routes[:] = [r for r in app.router.routes if not _is_fw(r)]


def _agent_data(name: str) -> AgentData:
    return AgentData(
        name=name,
        context_config=ContextConfig(),
        react_config=ReActConfig(),
    )


@pytest.fixture
def client(tmp_path, tmp_path_factory, monkeypatch):
    """独立 SQLite storage（角色两表经 bind_storage 复用其连接）+ 两个 router。

    Fixture 启动时把 ``reviewer-1`` 授予 reviewer 角色（role_store 两表建在
    同一个 storage SQLite 文件内，与 agents / team / market 同库，不污染业务库）；
    收尾收回，保证状态不泄漏。
    """
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'market.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)
            await market_store.ensure_market_tables(storage)
            await market_review_store.ensure_review_tables(storage)
            role_store.bind_storage(storage)
            await role_store.ensure_tables()
            await role_store.assign_role("reviewer-1", role_store.REVIEWER_ROLE)

    _run(_provision())

    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    _remove_framework_agent_routes(app)
    app.include_router(agent_router)
    app.include_router(market_router)
    with TestClient(app) as test_client:
        yield test_client

    _run(role_store.revoke_role("reviewer-1", role_store.REVIEWER_ROLE))


def _create(client, headers, **body) -> str:
    resp = client.post("/agent/", json={"name": "x", **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


# ---------------------------------------------------------------------------
# 1) GET /agent/market/my-role —— 角色自检
# ---------------------------------------------------------------------------


def test_my_role_endpoint(client):
    resp = client.get("/agent/market/my-role", headers=HDR_REVIEWER)
    assert resp.status_code == 200
    assert resp.json()["roles"] == ["reviewer"]

    resp = client.get("/agent/market/my-role", headers=HDR_USER)
    assert resp.status_code == 200
    assert resp.json()["roles"] == []


# ---------------------------------------------------------------------------
# 2) GET /agent/ —— 列表隔离 vs 管理员全量
# ---------------------------------------------------------------------------


def test_agent_list_isolation_for_normal_user(client):
    mine = _create(client, HDR_USER, name="mine")
    _create(client, HDR_ALICE, name="alice-agent")
    resp = client.get("/agent/", headers=HDR_USER)
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.json()["agents"]}
    assert mine in ids
    assert len(ids) == 1  # 只看自己的


def test_agent_list_all_for_reviewer(client):
    mine = _create(client, HDR_USER, name="mine")
    alice = _create(client, HDR_ALICE, name="alice-agent")
    resp = client.get("/agent/", headers=HDR_REVIEWER)
    assert resp.status_code == 200
    ids = {a["id"] for a in resp.json()["agents"]}
    assert mine in ids
    assert alice in ids  # reviewer 能看到他人的


# ---------------------------------------------------------------------------
# 3) 审批接口权限门禁
# ---------------------------------------------------------------------------


def test_reviews_forbidden_for_normal_user(client):
    resp = client.get("/agent/market/reviews", headers=HDR_USER)
    assert resp.status_code == 403


def test_reviews_allowed_for_reviewer(client):
    resp = client.get("/agent/market/reviews", headers=HDR_REVIEWER)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4) reviewers 管理接口 + 末位保护（复用既有 POST/PUT/DELETE）
# ---------------------------------------------------------------------------


def test_reviewers_management(client):
    # 非名单内用户写操作 403
    assert client.post(
        "/agent/market/reviewers", json={"user_ids": ["x"]}, headers=HDR_USER,
    ).status_code == 403
    assert client.delete(
        "/agent/market/reviewers/reviewer-1", headers=HDR_USER,
    ).status_code == 403

    # GET 公开（普通用户可查全名单）
    resp = client.get("/agent/market/reviewers", headers=HDR_USER)
    assert resp.status_code == 200
    assert any(r["user_id"] == "reviewer-1" for r in resp.json()["reviewers"])

    # 名单内 POST：批量新增，空白忽略、重复跳过（幂等）
    resp = client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["reviewer-2", "", "reviewer-2"]},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    ids = [r["user_id"] for r in resp.json()["reviewers"]]
    assert "reviewer-1" in ids and "reviewer-2" in ids

    # DELETE 在名单内 → 204，立即生效
    assert client.delete(
        "/agent/market/reviewers/reviewer-2", headers=HDR_REVIEWER,
    ).status_code == 204
    # DELETE 不在名单（已删）→ 404
    assert client.delete(
        "/agent/market/reviewers/reviewer-2", headers=HDR_REVIEWER,
    ).status_code == 404


def test_reviewers_last_one_and_empty_protection(client):
    # 现有名单（fixture 已含 reviewer-1 + 种子）
    original = [
        r["user_id"] for r in client.get(
            "/agent/market/reviewers", headers=HDR_REVIEWER,
        ).json()["reviewers"]
    ]
    assert "reviewer-1" in original

    # 缩到只剩 reviewer-1
    resp = client.put(
        "/agent/market/reviewers",
        json={"user_ids": ["reviewer-1"]},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200

    # 删最后一人 → 409
    assert client.delete(
        "/agent/market/reviewers/reviewer-1", headers=HDR_REVIEWER,
    ).status_code == 409
    # PUT 空清单 → 409
    assert client.put(
        "/agent/market/reviewers", json={"user_ids": []}, headers=HDR_REVIEWER,
    ).status_code == 409
    # PUT 全空白清单同样 409
    assert client.put(
        "/agent/market/reviewers",
        json={"user_ids": ["  ", ""]},
        headers=HDR_REVIEWER,
    ).status_code == 409

    # 还原原名单（含种子），保证 fixture teardown 安全（reviewer-1 非最后一人）
    client.put(
        "/agent/market/reviewers",
        json={"user_ids": original},
        headers=HDR_REVIEWER,
    )


# ---------------------------------------------------------------------------
# 5) role_store 底层
# ---------------------------------------------------------------------------


def test_role_store_unit(client):
    # has_role
    assert _run(role_store.has_role("reviewer-1", role_store.REVIEWER_ROLE)) is True
    assert _run(role_store.has_role("nobody", role_store.REVIEWER_ROLE)) is False
    # assign / revoke 幂等
    added = _run(role_store.assign_role("u-x", role_store.REVIEWER_ROLE))
    assert added == 1
    assert _run(role_store.has_role("u-x", role_store.REVIEWER_ROLE)) is True
    assert _run(role_store.revoke_role("u-x", role_store.REVIEWER_ROLE)) == "ok"
    # list_my_roles
    assert "reviewer" in _run(role_store.list_my_roles("reviewer-1"))
    assert _run(role_store.list_my_roles("nobody")) == []


def test_role_overwrite_empty_protection(client):
    res = _run(role_store.overwrite_role_members("test_role", []))
    assert res == "empty"


def test_role_last_one_protection(client):
    _run(role_store.overwrite_role_members("test_role", ["solo"]))
    assert _run(role_store.revoke_role("solo", "test_role")) == "last_one"
