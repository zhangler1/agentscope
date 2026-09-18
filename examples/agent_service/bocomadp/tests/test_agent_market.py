# -*- coding: utf-8 -*-
"""智能体市场 + 归属查询（GET /agent/owned）集成测试。

覆盖（走真实 HTTP / 真 SQLite storage，模式与 test_agent_routes_integration
一致）：

1. GET /agent/owned   —— 严格归属隔离：
   - 团长 / 普通智能体在，`is_team` 标记正确；自建成员独属其团不返回；
   - 别人的智能体、source='team' 的派生 worker 不出现；
2. GET /agent/market  —— 市场列表（**名单表语义**：agent_market 有行
   = 在市场，user_id 不再承载"平台"语义）：
   - 名单内全部出现（含 source='team' 的 worker，无 source 筛选）；
   - 名单外（未上架的个人智能体）绝不出现；
   - tag 筛选：未打标 tag=''（空串）；
3. GET /agent/market/featured —— 精选推荐（实时聚合 sessions）：
   - 按会话数倒序取前 N；0 热度是合法状态，不过滤；
4. POST /agent/market/{agent_id}/publish|unpublish —— 上架管理：
   - publish = 插名单行；unpublish = 删行（标签随行消失），均幂等；
   - 仅智能体 owner（X-User-ID = agents.user_id）可发布/撤回，
     智能体不存在 404；
5. PUT/DELETE /agent/market/{agent_id} —— 标签管理（全开放无权限
   门槛，**仅市场名单内智能体可打标**；撕标 = tag 置空）。

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
# 2) GET /agent/market —— 市场列表（agent_market 名单表驱动）
# ---------------------------------------------------------------------------


@pytest.fixture
def market_ids(client):
    """名单内 2 个（平台手动上架的 user 智能体 + team worker）+ 名单外 1 个。

    新口径：平台智能体由运营**手动 INSERT** 进 agent_market（代码不再
    自动补档）；worker 绕过 HTTP 直接插库模拟存量，同样手动上架；
    alice 的智能体不上架，绝不进市场。
    """
    platform_user = _create(client, HDR_ADMIN, name="平台应用A")
    storage = client.app.state.storage
    worker = AgentRecord(user_id="default", source="team", data=_agent_data("平台worker"))
    _run(storage.upsert_agent("default", worker))
    assert _run(market_store.insert_market_entry(storage, platform_user)) is True
    assert _run(market_store.insert_market_entry(storage, worker.id)) is True
    other = _create(client, HDR_ALICE, name="alice私有")
    return platform_user, worker.id, other


def test_market_scope_is_agent_market_rows(client, market_ids):
    """市场范围 = agent_market 名单（有行 = 在市场）。"""
    platform_user, worker_id, other_id = market_ids

    resp = client.get("/agent/market")
    assert resp.status_code == 200
    body = resp.json()
    ids = {a["id"] for a in body["agents"]}
    # 名单内的全进市场——包括 source='team' 的 worker（无 source 筛选）
    assert platform_user in ids
    assert worker_id in ids
    # 名单外（alice 未上架的智能体）绝不出现
    assert other_id not in ids
    assert body["total"] == 2


def test_market_tag_filter_and_untagged(client, market_ids):
    platform_user, worker_id, _ = market_ids
    # 新语义：手动上架未打标 → tag 为空串（前端"未分类"是纯展示文案）
    resp = client.get("/agent/market")
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[platform_user]["tag"] == ""
    assert by_id[worker_id]["tag"] == ""

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
    # 按未打标（空串）筛选：剩下没打过的
    resp = client.get("/agent/market", params={"tag": ""})
    assert [a["id"] for a in resp.json()["agents"]] == [worker_id]


def test_market_default_sort_updated_at_desc(client, market_ids):
    """市场列表默认按 updated_at 倒序（不按热度排）。"""
    platform_user, worker_id, _ = market_ids
    resp = client.get("/agent/market")
    ids = [a["id"] for a in resp.json()["agents"]]
    # worker 后上架，updated_at 更新 → 排前面
    assert ids == [worker_id, platform_user]


# ---------------------------------------------------------------------------
# 3) GET /agent/market/featured —— 实时热度聚合
# ---------------------------------------------------------------------------


def test_featured_orders_by_session_count(client, market_ids):
    platform_user, worker_id, other_id = market_ids
    _add_sessions(client.app.state.storage, platform_user, count=3)
    _add_sessions(client.app.state.storage, worker_id, count=1)
    # alice 名下的会话不参与市场热度统计
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
    # 会话数倒序：3 > 1；名单外的 alice 私有智能体不参与；热度值随行返回
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
# 4) 标签管理（全开放 + 仅名单内 + 自由字符串 + 撕标置空）
# ---------------------------------------------------------------------------


def test_tag_lifecycle_set_clear(client, market_ids):
    """标签生命周期：打标（自由字符串）→ 撕标（空串 = tag 置空）。"""
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
    # 响应体不再有 published / published_at（列已删）
    assert "published" not in resp.json()

    # 超长标签 → 422
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "x" * 65},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 422

    # 不存在的智能体 → 404
    resp = client.put(
        "/agent/market/no-such-agent",
        json={"tag": "XXX"},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 404

    # 撕标（空串）→ tag 置空（真正的"未打标"状态，名单行保留）
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": ""},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == ""
    assert resp.json()["created_at"] is not None


def test_market_tag_endpoint_open_and_delete(client, market_ids):
    """打标/撕标全开放：任何用户可调；DELETE=撕标（置空），幂等 204。"""
    platform_user, _, _ = market_ids

    # 打标
    resp = client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "XXX"},
        headers=HDR_ADMIN,
    )
    assert resp.status_code == 200

    # 撕标：204，幂等（再撕一次仍 204）
    resp = client.delete(f"/agent/market/{platform_user}", headers=HDR_USER)
    assert resp.status_code == 204
    resp = client.delete(f"/agent/market/{platform_user}", headers=HDR_USER)
    assert resp.status_code == 204
    # 撕标后市场里 tag 为空串（未打标）
    resp = client.get("/agent/market")
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[platform_user]["tag"] == ""


def test_tag_rejected_for_agents_not_in_market(client, market_ids):
    """不在市场名单内的智能体不能打标/撕标（打标不允许隐式上架）。"""
    _, _, other_id = market_ids

    # alice 私有智能体（未上架）→ 404
    resp = client.put(
        f"/agent/market/{other_id}", json={"tag": "预打标"}, headers=HDR_USER,
    )
    assert resp.status_code == 404
    assert client.delete(
        f"/agent/market/{other_id}", headers=HDR_USER,
    ).status_code == 404

    # 名单里也没有产生隐式行
    entry = _run(
        market_store.get_market_entry(client.app.state.storage, other_id),
    )
    assert entry is None


# ---------------------------------------------------------------------------
# 4.1) GET /agent/market/tags —— 全量标签清单（去重、升序、排除空串）
# ---------------------------------------------------------------------------


def test_market_tags_dedup_sorted_and_excludes_empty(client, market_ids):
    """清单 = agent_market.tag 现存量去重：升序、空串（未打标）不进。"""
    platform_user, worker_id, _ = market_ids

    # 初始全未打标 → 空清单（不返回 [""]）
    resp = client.get("/agent/market/tags")
    assert resp.status_code == 200
    assert resp.json() == {"tags": []}

    # 打两个标（含重复值场景：先打再改一个成相同值）
    assert client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "数据分析"},
        headers=HDR_USER,
    ).status_code == 200
    assert client.put(
        f"/agent/market/{worker_id}",
        json={"tag": "客服"},
        headers=HDR_USER,
    ).status_code == 200

    resp = client.get("/agent/market/tags")
    assert resp.json() == {"tags": ["客服", "数据分析"]}  # 升序

    # 撕标一个 → 从清单消失；未打标的 worker 行不影响清单
    assert client.delete(
        f"/agent/market/{platform_user}", headers=HDR_USER,
    ).status_code == 204
    assert client.get("/agent/market/tags").json() == {"tags": ["客服"]}


def test_market_tags_isolated_from_market_rows(client, market_ids):
    """只有名单内智能体的 tag 进清单；名单外智能体打不上标、不影响清单。"""
    _, _, other_id = market_ids

    # 名单外打标 404（不能隐式上架），清单仍为空
    assert client.put(
        f"/agent/market/{other_id}", json={"tag": "预打标"}, headers=HDR_USER,
    ).status_code == 404
    assert client.get("/agent/market/tags").json() == {"tags": []}


# ---------------------------------------------------------------------------
# 5) 热度统计范围（按 agent_id 聚合：外部用户的使用也要计入）
# ---------------------------------------------------------------------------


def test_heat_counts_sessions_from_any_user(client, market_ids):
    """热度按"被用的智能体"统计，不看会话的 user_id。

    修复前按 sessions.user_id='default' 筛 → 普通用户的使用全被漏掉；
    修复后按 agent_id ∈ 市场名单集合 聚合 → alice 用市场智能体也计入
    热度，而 alice 用自己的私有智能体不计入（本来就不在市场里）。
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
# 6) 级联清理：删智能体 → 市场名单行跟着删（防孤儿）
# ---------------------------------------------------------------------------


def test_delete_agent_cascades_market_entry(client, market_ids):
    """删智能体 → agent_market 里它的名单行被级联清理（不残留孤儿）。"""
    platform_user, _, _ = market_ids

    # 先打标
    assert client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "待删标签"},
        headers=HDR_ADMIN,
    ).status_code == 200

    # 删除前行在
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

    # 级联清理生效：名单行没了
    after = _run(
        market_store.get_market_entry(
            client.app.state.storage, platform_user,
        ),
    )
    assert after is None


# ---------------------------------------------------------------------------
# 7) 上架管理：publish = 插行 / unpublish = 删行（仅 owner）
# ---------------------------------------------------------------------------


def test_publish_unpublish_flow(client, market_ids):
    """个人智能体全流程：发布（插行）→ 市场可见 → 打标 → 撤回（删行）。

    撤回是**删名单行**：标签随行消失，重新发布后默认未打标、需重打。
    """
    _, _, other_id = market_ids
    storage = client.app.state.storage

    # 发布前：不在市场
    resp = client.get("/agent/market")
    assert other_id not in {a["id"] for a in resp.json()["agents"]}

    # owner 发布 → 200，插入名单行；新上架默认未打标
    resp = client.post(f"/agent/market/{other_id}/publish", headers=HDR_ALICE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == other_id
    assert body["tag"] == ""
    # 响应体不再有 published / published_at（列已删）
    assert "published" not in body

    # 市场出现：名单原有 2 个 + 发布的 1 个
    resp = client.get("/agent/market")
    data = resp.json()
    assert other_id in {a["id"] for a in data["agents"]}
    assert data["total"] == 3

    # 打标跟着进市场
    resp = client.put(
        f"/agent/market/{other_id}", json={"tag": "数据分析"}, headers=HDR_USER,
    )
    assert resp.status_code == 200
    resp = client.get("/agent/market", params={"tag": "数据分析"})
    assert [a["id"] for a in resp.json()["agents"]] == [other_id]

    # owner 撤回 → 204，名单行删除（标签随行消失）
    resp = client.post(f"/agent/market/{other_id}/unpublish", headers=HDR_ALICE)
    assert resp.status_code == 204

    resp = client.get("/agent/market")
    assert other_id not in {a["id"] for a in resp.json()["agents"]}
    entry = _run(market_store.get_market_entry(storage, other_id))
    assert entry is None

    # 幂等：重复撤回仍 204
    assert client.post(
        f"/agent/market/{other_id}/unpublish", headers=HDR_ALICE,
    ).status_code == 204

    # 重新发布：名单行重建，标签为空（旧标签已随删行消失）
    resp = client.post(f"/agent/market/{other_id}/publish", headers=HDR_ALICE)
    assert resp.status_code == 200
    assert resp.json()["tag"] == ""


def test_publish_owner_only(client, market_ids):
    """发布/撤回都锁 owner：别人的 X-User-ID 一律 403。"""
    _, _, other_id = market_ids

    # 未发布时别人不能替 alice 发布
    assert client.post(
        f"/agent/market/{other_id}/publish", headers=HDR_USER,
    ).status_code == 403

    # alice 自己发布成功后，别人也不能撤回
    assert client.post(
        f"/agent/market/{other_id}/publish", headers=HDR_ALICE,
    ).status_code == 200
    assert client.post(
        f"/agent/market/{other_id}/unpublish", headers=HDR_USER,
    ).status_code == 403
    # 市场里还在（撤回被拒）
    assert other_id in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }


def test_publish_guard_rules(client, market_ids):
    """不存在 404，非 owner 403，owner 重复发布幂等。"""
    platform_user, _, _ = market_ids

    # 名单内的平台智能体：非 owner 发布/撤回 403
    assert client.post(
        f"/agent/market/{platform_user}/publish", headers=HDR_ALICE,
    ).status_code == 403
    assert client.post(
        f"/agent/market/{platform_user}/unpublish", headers=HDR_ALICE,
    ).status_code == 403

    # owner 重复发布幂等：不覆盖已有标签
    assert client.put(
        f"/agent/market/{platform_user}",
        json={"tag": "运营打的标"},
        headers=HDR_ADMIN,
    ).status_code == 200
    resp = client.post(
        f"/agent/market/{platform_user}/publish", headers=HDR_ADMIN,
    )
    assert resp.status_code == 200
    assert resp.json()["tag"] == "运营打的标"

    # 不存在：404
    assert client.post(
        "/agent/market/no-such-agent/publish", headers=HDR_ALICE,
    ).status_code == 404


def test_published_agent_in_featured(client, market_ids):
    """个人发布的智能体参与精选热度排序，与其他市场成员同台竞技。"""
    _, _, other_id = market_ids
    assert client.post(
        f"/agent/market/{other_id}/publish", headers=HDR_ALICE,
    ).status_code == 200
    _add_sessions(client.app.state.storage, other_id, count=2)

    resp = client.get("/agent/market/featured", params={"top": 3})
    assert resp.status_code == 200
    body = resp.json()
    # 热度 2 > 平台智能体的 0 → 排第一
    assert body["agents"][0]["id"] == other_id
    assert body["agents"][0]["heat"] == 2
    assert body["total"] == 3
