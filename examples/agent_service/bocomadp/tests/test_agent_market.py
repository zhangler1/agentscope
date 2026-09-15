# -*- coding: utf-8 -*-
"""智能体市场 + 归属查询（GET /agent/owned）集成测试。

覆盖（走真实 HTTP / 真 SQLite storage，模式与 test_agent_routes_integration
一致）：

1. GET /agent/owned   —— 严格归属隔离：
   - 团长 / 普通智能体在，`is_team` 标记正确；自建成员独属其团不返回；
   - 别人的智能体、source='team' 的派生 worker 不出现；
2. GET /agent/market  —— 平台市场：
   - 范围只由 user_id='default' 决定，**无 source 筛选**（worker 也进市场）；
   - 别的用户的智能体绝不出现；
   - tag 筛选 + 未打标 tag=null 语义；
3. GET /agent/market/featured —— 精选推荐（方案 A：实时聚合 sessions）：
   - 按会话数倒序取前 N；0 热度是合法状态，不过滤；
4. PUT/DELETE /agent/market/{agent_id} —— 标签管理（全开放无权限
   门槛，tag 自由字符串无清单校验）。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与
test_expert_team.py 一致）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import create_app
from agentscope.app._router._agent import (
    agent_router as _framework_agent_router,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AgentData, AgentRecord, AsyncSQLAlchemyStorage
from agentscope.app.storage._sql._tables import SessionRow
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import market_store, team_store
from bocomadp.routers.agent import agent_router
from bocomadp.routers.market import market_router

HDR_USER = {"X-User-ID": "test-user"}      # 普通用户（不在管理员白名单）
HDR_ALICE = {"X-User-ID": "alice"}         # 另一个普通用户
HDR_ADMIN = {"X-User-ID": "default"}       # 平台运营（config 默认白名单内）


def _remove_framework_agent_routes(app) -> None:
    """摘除框架内置 /agent 路由（main.py / test_agent_routes_integration 同款）。"""
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


def _run(coro):
    return asyncio.run(coro)


def _agent_data(name: str) -> AgentData:
    """构造合法 AgentData（context/react config 是必填字段）。"""
    return AgentData(
        name=name,
        context_config=ContextConfig(),
        react_config=ReActConfig(),
    )


def _add_sessions(storage, agent_id: str, count: int) -> None:
    """直接往 sessions 表插流水（featured 实时聚合的数据源）。"""

    async def _go() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with storage._session_factory() as session:
            for i in range(count):
                session.add(
                    SessionRow(
                        id=f"s-{agent_id}-{i}-{uuid.uuid4().hex[:8]}",
                        user_id="default",
                        agent_id=agent_id,
                        source="user",
                        payload={},
                        created_at=now,
                        updated_at=now,
                    ),
                )
            await session.commit()

    _run(_go())


@pytest.fixture
def client(tmp_path):
    """sqlite 存储 + 团队/市场两张 bocomadp 自建表 + 两个 router。"""
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'market.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)
            await market_store.ensure_market_tables(storage)

    _run(_provision())

    # 完整 app 装配（与 test_agent_routes_integration 一致）：create_app
    # 会填好 state.resource_access_service 等全部依赖，POST /agent/ 才能用。
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


# ---------------------------------------------------------------------------
# 1) GET /agent/owned —— 严格归属隔离
# ---------------------------------------------------------------------------


def _create(client, headers, **body) -> str:
    resp = client.post("/agent/", json={"name": "x", **body}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


def test_owned_returns_own_agents_with_team_markers(client):
    leader_id = _create(client, HDR_USER, name="leader", is_team=True)
    member_id = _create(client, HDR_USER, name="member", parent_agent_id=leader_id)
    solo_id = _create(client, HDR_USER, name="solo")

    resp = client.get("/agent/owned", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    by_id = {a["id"]: a for a in body["agents"]}

    # 自建成员独属其团，不与团长/独立智能体并列返回
    assert member_id not in by_id
    assert body["total"] == 2

    # 团长：is_team=true
    assert by_id[leader_id]["is_team"] is True
    assert by_id[leader_id]["parent_agent_id"] is None
    # 普通智能体：无任何团队标记
    assert by_id[solo_id]["is_team"] is False
    assert by_id[solo_id]["parent_agent_id"] is None


def test_owned_excludes_others_and_team_workers(client, tmp_path):
    _create(client, HDR_USER, name="mine")
    # 别人的智能体（就算将来共享给我，也不属于"我名下"）
    alice_id = _create(client, HDR_ALICE, name="alice-agent")
    # source='team' 的派生 worker（运行时生成，非用户资产）
    storage = client.app.state.storage
    _run(
        storage.upsert_agent(
            "test-user",
            AgentRecord(user_id="test-user", source="team", data=_agent_data("worker")),
        ),
    )

    resp = client.get("/agent/owned", headers=HDR_USER)
    ids = {a["id"] for a in resp.json()["agents"]}
    assert alice_id not in ids
    # 只剩 mine（worker 被 source='user' 过滤）
    assert len(ids) == 1

    # alice 只能查到自己的
    resp = client.get("/agent/owned", headers=HDR_ALICE)
    assert [a["id"] for a in resp.json()["agents"]] == [alice_id]


def test_owned_pagination(client):
    for i in range(5):
        _create(client, HDR_USER, name=f"a{i}")
    resp = client.get("/agent/owned", params={"pageNum": 2, "pageSize": 2}, headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["agents"]) == 2


# ---------------------------------------------------------------------------
# 2) GET /agent/market —— 平台市场（user_id=default，无 source 筛选）
# ---------------------------------------------------------------------------


@pytest.fixture
def market_ids(client):
    """平台名下 2 个（user + team worker）+ 别的用户 1 个。

    worker 绕过 HTTP 直接插库（模拟历史存量），补一次默认标签扫描
    对齐"平台智能体自动建档"语义。
    """
    platform_user = _create(client, HDR_ADMIN, name="平台应用A")
    storage = client.app.state.storage
    worker = AgentRecord(user_id="default", source="team", data=_agent_data("平台worker"))
    _run(storage.upsert_agent("default", worker))
    _run(market_store.ensure_default_tags(storage))
    other = _create(client, HDR_ALICE, name="alice私有")
    return platform_user, worker.id, other


def test_market_scope_is_default_user_only(client, market_ids):
    platform_user, worker_id, other_id = market_ids

    resp = client.get("/agent/market")
    assert resp.status_code == 200
    body = resp.json()
    ids = {a["id"] for a in body["agents"]}
    # 平台名下全进市场——包括 source='team' 的 worker（无 source 筛选）
    assert platform_user in ids
    assert worker_id in ids
    # 别的用户（alice）的智能体绝不出现在平台市场
    assert other_id not in ids
    assert body["total"] == 2


def test_market_excludes_system_agents(client):
    """系统内置智能体（_ 开头 id）不进市场、不可打标。

    如 _agent-creator（智能体工厂）挂在 default 名下，属内部工具
    载体，不应作为市场商品对用户露出。
    """
    storage = client.app.state.storage
    sys_agent = AgentRecord(
        id="_factory-x",
        user_id="default",
        data=_agent_data("内置工具"),
    )
    _run(storage.upsert_agent("default", sys_agent))

    # 市场列表与精选都不出现
    resp = client.get("/agent/market")
    assert all(not a["id"].startswith("_") for a in resp.json()["agents"])
    resp = client.get("/agent/market/featured")
    assert all(not a["id"].startswith("_") for a in resp.json()["agents"])

    # 打标 → 422 拒绝
    resp = client.put(
        "/agent/market/_factory-x",
        json={"tag": "XXX"},
        headers=HDR_USER,
    )
    assert resp.status_code == 422


def test_market_tag_filter_and_untagged(client, market_ids):
    platform_user, worker_id, _ = market_ids
    # 新语义：平台智能体创建时自动写入默认标签"未分类"
    resp = client.get("/agent/market")
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[platform_user]["tag"] == "未分类"
    assert by_id[worker_id]["tag"] == "未分类"

    # 覆盖新标签（任何用户都能打，无权限门槛）
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "XXX"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == "XXX"

    # 按标签筛选：只有打了 XXX 的命中
    resp = client.get("/agent/market", params={"tag": "XXX"})
    assert [a["id"] for a in resp.json()["agents"]] == [platform_user]
    # 按默认标签筛选：剩下没改过的
    resp = client.get("/agent/market", params={"tag": "未分类"})
    assert [a["id"] for a in resp.json()["agents"]] == [worker_id]


def test_market_default_sort_updated_at_desc(client, market_ids):
    """市场列表默认按 updated_at 倒序（不按热度排）。"""
    platform_user, worker_id, _ = market_ids
    resp = client.get("/agent/market")
    ids = [a["id"] for a in resp.json()["agents"]]
    # worker 后建，updated_at 更新 → 排前面
    assert ids == [worker_id, platform_user]


# ---------------------------------------------------------------------------
# 3) GET /agent/market/featured —— 实时热度聚合
# ---------------------------------------------------------------------------


def test_featured_orders_by_session_count(client, market_ids):
    platform_user, worker_id, other_id = market_ids
    _add_sessions(client.app.state.storage, platform_user, count=3)
    _add_sessions(client.app.state.storage, worker_id, count=1)
    # alice 名下的会话不参与平台热度统计
    storage = client.app.state.storage

    async def _alice_session() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with storage._session_factory() as session:
            session.add(
                SessionRow(
                    id=f"s-{other_id}-x",
                    user_id="alice",
                    agent_id=other_id,
                    source="user",
                    payload={},
                    created_at=now,
                    updated_at=now,
                ),
            )
            await session.commit()

    _run(_alice_session())

    resp = client.get("/agent/market/featured", params={"top": 2})
    assert resp.status_code == 200
    body = resp.json()
    # 会话数倒序：3 > 1；alice 的不参与；热度值随行返回
    assert [a["id"] for a in body["agents"]] == [platform_user, worker_id]
    heats = {a["id"]: a["heat"] for a in body["agents"]}
    assert heats == {platform_user: 3, worker_id: 1}


def test_featured_zero_heat_is_valid_and_fills_slots(client, market_ids):
    """刚发布没人用：热度 0 是正常状态，不过滤、按 updated_at 兜底排序。"""
    platform_user, worker_id, _ = market_ids
    resp = client.get("/agent/market/featured", params={"top": 4})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2  # 取不满 4 个就返回实际条数
    assert all(a["heat"] == 0 for a in body["agents"])
    assert [a["id"] for a in body["agents"]] == [worker_id, platform_user]


# ---------------------------------------------------------------------------
# 4) 标签管理（全开放 + 自由字符串 + 级联清理）
# ---------------------------------------------------------------------------


def test_tag_lifecycle_set_clear(client, market_ids):
    """标签生命周期：打标（自由字符串）→ 清标（空串 = 重置回"未分类"）。"""
    platform_user, _, _ = market_ids

    # 打标：自由字符串，任何值都收（无清单校验），任何用户可调
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "随便什么标签"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == "随便什么标签"
    assert resp.json()["created_at"] is not None

    # 超长标签 → 422
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "x" * 65},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 422

    # 平台名下不存在的智能体 → 404
    resp = client.put(
        "/agent/market/no-such-agent",
        json={"tag": "XXX"},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 404

    # 清标（空串）→ 重置回默认标签"未分类"（档案行保留）
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": ""},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == "未分类"
    assert resp.json()["created_at"] is not None


def test_market_tag_endpoint_open_and_delete(client, market_ids):
    """打标/下架全开放：任何用户可调；下架=重置默认标签，幂等 204。"""
    platform_user, _, _ = market_ids

    # 打标
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "XXX"},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 200

    # 下架：204，幂等（再删一次仍 204）
    resp = client.delete(f"/agent/market/{platform_user}", headers=HDR_USER)
    assert resp.status_code == 204
    resp = client.delete(f"/agent/market/{platform_user}", headers=HDR_USER)
    assert resp.status_code == 204
    # 下架后市场里 tag 回到默认"未分类"
    resp = client.get("/agent/market")
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[platform_user]["tag"] == "未分类"

    # 系统内置智能体 → 422 拒绝
    assert client.put(
        "/agent/market/_agent-creator", json={"tag": "XXX"}, headers=HDR_ADMIN,
    ).status_code == 422
    assert client.delete(
        "/agent/market/_agent-creator", headers=HDR_ADMIN,
    ).status_code == 422


# ---------------------------------------------------------------------------
# 5) 热度统计范围（按 agent_id 聚合：外部用户的使用也要计入）
# ---------------------------------------------------------------------------


def test_heat_counts_sessions_from_any_user(client, market_ids):
    """热度按"被用的智能体"统计，不看会话的 user_id。

    修复前按 sessions.user_id='default' 筛 → 普通用户的使用全被漏掉；
    修复后按 agent_id ∈ 平台集合 聚合 → alice 用平台智能体也计入热度，
    而 alice 用自己的私有智能体不计入（本来就不在市场里）。
    """
    platform_user, worker_id, other_id = market_ids
    storage = client.app.state.storage

    async def _seed() -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with storage._session_factory() as session:
            session.add(
                SessionRow(
                    id="s-alice-uses-platform",
                    user_id="alice",
                    agent_id=platform_user,
                    source="user",
                    payload={},
                    created_at=now,
                    updated_at=now,
                ),
            )
            session.add(
                SessionRow(
                    id="s-alice-uses-own",
                    user_id="alice",
                    agent_id=other_id,
                    source="user",
                    payload={},
                    created_at=now,
                    updated_at=now,
                ),
            )
            await session.commit()

    _run(_seed())

    resp = client.get("/agent/market/featured", params={"top": 4})
    heats = {a["id"]: a["heat"] for a in resp.json()["agents"]}
    assert heats[platform_user] == 1   # 外部用户的使用计入（修复点）
    assert heats[worker_id] == 0       # 无会话
    assert other_id not in heats       # 别人的智能体不进市场


# ---------------------------------------------------------------------------
# 6) 级联清理：删智能体 → 市场档案跟着删（防孤儿档案）
# ---------------------------------------------------------------------------


def test_delete_agent_cascades_market_entry(client, market_ids):
    """删智能体 → agent_market 里它的档案被级联清理（不残留孤儿）。"""
    platform_user, _, _ = market_ids

    # 先打标建档
    assert client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "待删标签"},
        headers=HDR_ADMIN,
    ).status_code == 200

    # 删除前档案在
    before = _run(
        market_store.get_market_entry(
            client.app.state.storage, platform_user,
        ),
    )
    assert before is not None and before.tag == "待删标签"

    # 删除智能体（X-User-ID 必须是拥有者 default）
    assert client.delete(
        f"/agent/{platform_user}", headers=HDR_ADMIN,
    ).status_code == 204

    # 级联清理生效：档案没了
    after = _run(
        market_store.get_market_entry(
            client.app.state.storage, platform_user,
        ),
    )
    assert after is None
