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
4. POST /agent/market/{agent_id}/publish|unpublish —— 发布走审批流：
   - publish = 写审批表 pending（幂等，**不再直接上架**）；审批人
     approve 后才插名单行；unpublish = 删市场行 + 清审批记录，幂等；
   - 仅智能体 owner（X-User-ID = agents.user_id）可发布/撤回，
     智能体不存在 404；
5. PUT/DELETE /agent/market/{agent_id} —— 标签管理（全开放无权限
   门槛，**仅市场名单内智能体可打标**；撕标 = tag 置空）；
6. GET/POST/PUT/DELETE /agent/market/reviewers —— 审批人白名单管理
   （JSON 文件 + 末位保护：PUT 传空 / 删最后一个 → 409；无锚点，
   生效名单 = 环境变量指向的 JSON 文件内容）。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与
test_expert_team.py 一致）。
"""
from __future__ import annotations

import asyncio
import json
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

from bocomadp import (
    market_review_store,
    market_reviewers,
    market_store,
    team_store,
)
from bocomadp.routers.agent import agent_router
from bocomadp.routers.market import market_router

HDR_USER = {"X-User-ID": "test-user"}      # 普通用户（不在管理员白名单）
HDR_ALICE = {"X-User-ID": "alice"}         # 另一个普通用户
HDR_ADMIN = {"X-User-ID": "default"}       # 平台运营（config 默认白名单内）
HDR_REVIEWER = {"X-User-ID": "reviewer-1"}  # 市场审批人（fixture 种入名单）

# 发布弹窗表单（publish 必带：部门/系统/业务条线/说明；业务条线即
# 市场标签 tag）
_PUB_BODY = {
    "department": "网络金融部",
    "system_name": "智能体平台",
    "tag": "智能研发",
    "description": "用于测试的智能体说明",
}


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
def client(tmp_path, monkeypatch):
    """sqlite 存储 + 团队/市场两张 bocomadp 自建表 + 两个 router。

    审批人白名单：环境变量指向 tmp 文件并种入 ``reviewer-1`` 一人
    （模拟"运维手工种入首批名单"的部署动作），加载后整个测试期内
    生效；收尾清空模块级 set，防止状态泄漏到其他测试文件。
    """
    reviewers_file = tmp_path / "reviewers.json"
    monkeypatch.setenv("BOCOMADP_MARKET_REVIEWERS_FILE", str(reviewers_file))
    reviewers_file.write_text(
        json.dumps(["reviewer-1"]), encoding="utf-8",
    )
    market_reviewers.load_whitelist()

    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'market.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)
            await market_store.ensure_market_tables(storage)
            await market_review_store.ensure_review_tables(storage)

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

    # 收尾：清掉模块级白名单状态（env 由 monkeypatch 自动还原）
    market_reviewers._reviewers.clear()


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
    """个人智能体全流程：发布（pending）→ 审批通过上架 → 打标 → 撤回。

    新口径：publish **不再直接上架**，写审批表 pending；审批人 approve
    后才插名单行。撤回 = 删市场行 + 清审批记录（标签随行消失）。
    """
    _, _, other_id = market_ids
    storage = client.app.state.storage

    # 发布前：不在市场
    resp = client.get("/agent/market")
    assert other_id not in {a["id"] for a in resp.json()["agents"]}

    # owner 发布 → 200，进入审批 pending（不再直接上架）
    resp = client.post(f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == other_id
    assert body["status"] == "pending"
    assert body["applicant"] == "alice"

    # 待审批期间市场不可见
    resp = client.get("/agent/market")
    assert other_id not in {a["id"] for a in resp.json()["agents"]}

    # 审批人通过 → 状态 approved + 插名单行
    resp = client.post(
        f"/agent/market/reviews/{other_id}/approve", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    # 市场出现：名单原有 2 个 + 审批上架的 1 个
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
    # 审批记录也被级联清掉（回到"未提交"）
    review = _run(market_review_store.get_review_record(storage, other_id))
    assert review is None

    # 幂等：重复撤回仍 204
    assert client.post(
        f"/agent/market/{other_id}/unpublish", headers=HDR_ALICE,
    ).status_code == 204

    # 重新发布：回到 pending（重新走审批）
    resp = client.post(f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE)
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


def test_publish_owner_only(client, market_ids):
    """发布仍锁 owner（别人的 X-User-ID 一律 403）；
    **下架/撤回全放开**：非 owner 可撤别人的 pending 申请。
    """
    _, _, other_id = market_ids

    # 未发布时别人不能替 alice 发布
    assert client.post(
        f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_USER,
    ).status_code == 403

    # alice 发布（pending）后，别人可以撤回（市场操作人人有权限）
    assert client.post(
        f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200
    assert client.post(
        f"/agent/market/{other_id}/unpublish", headers=HDR_USER,
    ).status_code == 204
    # 撤回生效：审批记录被清掉，市场里没有
    review = _run(
        market_review_store.get_review_record(
            client.app.state.storage, other_id,
        ),
    )
    assert review is None
    assert other_id not in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }


def test_publish_guard_rules(client, market_ids):
    """不存在 404，发布仍非 owner 403，已在市场的智能体重复发布幂等；
    **下架人人可操作**：非 owner 下架已上架的智能体 204。
    """
    platform_user, _, _ = market_ids

    # 名单内的平台智能体：非 owner 发布 403
    assert client.post(
        f"/agent/market/{platform_user}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 403

    # owner 重复发布幂等：已在市场 → 直接返回 approved，不重复审批
    resp = client.post(
        f"/agent/market/{platform_user}/publish", json=_PUB_BODY, headers=HDR_ADMIN,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    # 名单里没有产生重复行
    assert client.get("/agent/market").json()["total"] == 2

    # 不存在：404
    assert client.post(
        "/agent/market/no-such-agent/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 404
    assert client.post(
        "/agent/market/no-such-agent/unpublish", headers=HDR_ALICE,
    ).status_code == 404

    # 非 owner 下架：全放开，204（市场行被删，unpublish 幂等）
    assert client.post(
        f"/agent/market/{platform_user}/unpublish", headers=HDR_ALICE,
    ).status_code == 204
    assert client.post(
        f"/agent/market/{platform_user}/unpublish", headers=HDR_ALICE,
    ).status_code == 204
    assert platform_user not in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }


def test_published_agent_in_featured(client, market_ids):
    """个人发布（审批通过上架）的智能体参与精选热度排序。"""
    _, _, other_id = market_ids
    assert client.post(
        f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200
    assert client.post(
        f"/agent/market/reviews/{other_id}/approve", headers=HDR_REVIEWER,
    ).status_code == 200
    _add_sessions(client.app.state.storage, other_id, count=2)

    resp = client.get("/agent/market/featured", params={"top": 3})
    assert resp.status_code == 200
    body = resp.json()
    # 热度 2 > 平台智能体的 0 → 排第一
    assert body["agents"][0]["id"] == other_id
    assert body["agents"][0]["heat"] == 2
    assert body["total"] == 3


# ---------------------------------------------------------------------------
# 6) 审批人白名单（GET 公开；写操作仅名单内；末位保护 409）
# ---------------------------------------------------------------------------


def test_reviewer_whitelist_crud(client):
    """GET 公开 → POST 批量幂等 → PUT 全量覆盖 → DELETE 单删 → 403/404。"""
    # GET 公开（无需名单内）：fixture 种入 1 人
    resp = client.get("/agent/market/reviewers", headers=HDR_USER)
    assert resp.status_code == 200
    assert resp.json() == {"reviewers": [{"user_id": "reviewer-1"}]}

    # 非名单内用户写操作 403
    assert client.post(
        "/agent/market/reviewers", json={"user_ids": ["x"]}, headers=HDR_USER,
    ).status_code == 403
    assert client.delete(
        "/agent/market/reviewers/reviewer-1", headers=HDR_USER,
    ).status_code == 403

    # 名单内 POST：批量新增，空白忽略、重复跳过（幂等）
    resp = client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["reviewer-2", "reviewer-3", "", "reviewer-2"]},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    assert [r["user_id"] for r in resp.json()["reviewers"]] == [
        "reviewer-1", "reviewer-2", "reviewer-3",
    ]

    # PUT 全量覆盖
    resp = client.put(
        "/agent/market/reviewers",
        json={"user_ids": ["reviewer-2", "reviewer-1"]},
        headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    assert [r["user_id"] for r in resp.json()["reviewers"]] == [
        "reviewer-1", "reviewer-2",
    ]

    # DELETE 在名单内 → 204，立即生效
    assert client.delete(
        "/agent/market/reviewers/reviewer-2", headers=HDR_REVIEWER,
    ).status_code == 204
    assert [r["user_id"] for r in client.get(
        "/agent/market/reviewers", headers=HDR_REVIEWER,
    ).json()["reviewers"]] == ["reviewer-1"]

    # DELETE 不在名单（已删）→ 404
    assert client.delete(
        "/agent/market/reviewers/reviewer-2", headers=HDR_REVIEWER,
    ).status_code == 404


def test_reviewer_whitelist_last_one_protection(client):
    """末位保护：删最后一个 / PUT 清空（含全空白）→ 409，名单保持非空。"""
    # 删最后一个审批人 → 409
    resp = client.delete(
        "/agent/market/reviewers/reviewer-1", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 409, resp.text
    assert "至少保留一个审批人" in resp.json()["detail"]

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

    # 名单原封不动，审批权还在
    assert [r["user_id"] for r in client.get(
        "/agent/market/reviewers", headers=HDR_REVIEWER,
    ).json()["reviewers"]] == ["reviewer-1"]


def test_reviewer_whitelist_persist_and_reload(client, tmp_path):
    """名单原子落盘（重启不丢）：磁盘 JSON 与内存一致，重载恢复。"""
    reviewers_file = tmp_path / "reviewers.json"
    client.post(
        "/agent/market/reviewers",
        json={"user_ids": ["reviewer-9"]},
        headers=HDR_REVIEWER,
    )
    on_disk = json.loads(reviewers_file.read_text(encoding="utf-8"))
    assert sorted(on_disk) == ["reviewer-1", "reviewer-9"]

    # 模拟重启：清内存重载 → 名单从文件恢复
    market_reviewers._reviewers.clear()
    market_reviewers.load_whitelist()
    assert market_reviewers.effective_reviewers() == [
        "reviewer-1", "reviewer-9",
    ]


def test_review_apis_open_to_all_users(client, market_ids):
    """审批三件套（列表/通过/拒绝）不校验白名单，普通用户也能调。

    若日后启用审批人白名单，本测试应改回断言 403。
    """
    _, _, other_id = market_ids
    client.post(f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE)

    # 列表 + 拒绝（pending → rejected）普通用户均可调
    cases = [
        ("get", "/agent/market/reviews", {}),
        ("post", f"/agent/market/reviews/{other_id}/reject",
         {"json": {"reason": "不通过"}}),
    ]
    for method, path, kwargs in cases:
        resp = getattr(client, method)(path, headers=HDR_USER, **kwargs)
        assert resp.status_code == 200, (method, path, resp.text)

    # rejected 重新 publish 回 pending 后，普通用户也能 approve
    client.post(f"/agent/market/{other_id}/publish", json=_PUB_BODY, headers=HDR_ALICE)
    resp = client.post(
        f"/agent/market/reviews/{other_id}/approve", headers=HDR_USER,
    )
    assert resp.status_code == 200, resp.text


def test_reviewer_seed_on_missing_file(monkeypatch, tmp_path):
    """种子名单：仅当名单文件不存在时一次性写入落盘，之后文件为准。"""
    reviewers_file = tmp_path / "seed.json"
    monkeypatch.setenv("BOCOMADP_MARKET_REVIEWERS_FILE", str(reviewers_file))
    monkeypatch.setattr(
        market_reviewers, "SEED_REVIEWERS", ("seed-b", "seed-a", "  "),
    )
    market_reviewers._reviewers.clear()
    try:
        # 首次加载：文件缺失 → 种子写入（空白项剔除）+ 落盘
        market_reviewers.load_whitelist()
        assert market_reviewers.effective_reviewers() == ["seed-a", "seed-b"]
        assert json.loads(reviewers_file.read_text(encoding="utf-8")) == [
            "seed-a", "seed-b",
        ]

        # 模拟重启：文件已存在 → 直接从文件恢复（不重复播种）
        market_reviewers.load_whitelist()
        assert market_reviewers.effective_reviewers() == ["seed-a", "seed-b"]

        # 文件存在但为空数组（运维有意清场）→ 尊重文件，不重新播种
        reviewers_file.write_text("[]", encoding="utf-8")
        market_reviewers.load_whitelist()
        assert market_reviewers.effective_reviewers() == []
    finally:
        market_reviewers._reviewers.clear()


# ---------------------------------------------------------------------------
# 7) 审核工作台查询：全部状态 / keyword 模糊 / status_counts / 详情接口
#    / approve 选填意见 / /agent/ 列表状态字段
# ---------------------------------------------------------------------------


def _publish_and_decide(
    client,
    agent_id: str,
    owner_hdr: dict,
    decision: str,
    reason: str | None = None,
) -> dict:
    """publish → approve/reject 一条龙（测试脚手架），返回决定响应体。"""
    assert client.post(
        f"/agent/market/{agent_id}/publish",
        json=_PUB_BODY,
        headers=owner_hdr,
    ).status_code == 200
    if decision == "approve":
        resp = client.post(
            f"/agent/market/reviews/{agent_id}/approve",
            json={"reason": reason} if reason else None,
            headers=HDR_REVIEWER,
        )
    else:
        resp = client.post(
            f"/agent/market/reviews/{agent_id}/reject",
            json={"reason": reason or "不符合上架要求"},
            headers=HDR_REVIEWER,
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_reviews_list_all_and_status_counts(client):
    """status 不传 = 全部三种状态（提交时间倒序）；status_counts 全量计数。"""
    a_pending = _create(client, HDR_ALICE, name="待审核应用")
    a_ok = _create(client, HDR_ALICE, name="已通过应用")
    a_no = _create(client, HDR_ALICE, name="已驳回应用")
    _publish_and_decide(client, a_pending, HDR_ALICE, "reject")   # 先拒
    _publish_and_decide(client, a_ok, HDR_ALICE, "approve")       # 再批
    # 重新发布待审核的，制造 pending（重发不刷新 created_at——老口径）
    assert client.post(
        f"/agent/market/{a_pending}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200

    # 不传 status = 全部
    resp = client.get("/agent/market/reviews", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2  # a_pending + a_ok（单记录覆盖，a_no 被重发覆盖）
    statuses = {r["agent_id"]: r["status"] for r in body["reviews"]}
    assert statuses == {a_pending: "pending", a_ok: "approved"}
    # 全部 tab：按申请时间（首次提交）倒序——a_pending 首提最早、a_ok
    # 首提更晚 → a_ok 在前
    assert [r["agent_id"] for r in body["reviews"]] == [a_ok, a_pending]
    # 统计卡：全量计数，不随筛选变化
    assert body["status_counts"] == {
        "pending": 1, "approved": 1, "rejected": 0, "all": 2,
    }

    # status=all 等价于不传
    resp_all = client.get(
        "/agent/market/reviews", params={"status": "all"}, headers=HDR_USER,
    )
    assert resp_all.json()["total"] == 2

    # 单状态筛选照常可用（pending 正序先到先审 / 已办按审批时间倒序）
    resp = client.get(
        "/agent/market/reviews", params={"status": "approved"}, headers=HDR_USER,
    )
    assert [r["agent_id"] for r in resp.json()["reviews"]] == [a_ok]


def test_reviews_keyword_fuzzy_matches_name_or_applicant(client):
    """keyword 单字段模糊：名称 OR 提交人任一包含即命中（大小写不敏感）。"""
    hit1 = _create(client, HDR_ALICE, name="授信报告智能生成")
    hit2 = _create(client, HDR_USER, name="公文写作助手")
    miss = _create(client, HDR_ALICE, name="客服话术推荐")
    # 各自的 owner 发布（publish 仍锁 owner）
    assert client.post(
        f"/agent/market/{hit1}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200
    assert client.post(
        f"/agent/market/{hit2}/publish", json=_PUB_BODY, headers=HDR_USER,
    ).status_code == 200
    assert client.post(
        f"/agent/market/{miss}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200

    def _ids(kw: str) -> set[str]:
        resp = client.get(
            "/agent/market/reviews",
            params={"keyword": kw, "pageSize": 50},
            headers=HDR_USER,
        )
        assert resp.status_code == 200
        return {r["agent_id"] for r in resp.json()["reviews"]}

    # 名称模糊命中
    assert _ids("授信") == {hit1}
    # 提交人模糊命中（alice 的两个申请，名称各不相同）
    assert _ids("alice") == {hit1, miss}
    # 大小写不敏感
    assert _ids("ALICE") == {hit1, miss}
    # 名称 OR 提交人：谁都不含 → 空
    assert _ids("不存在的关键词") == set()
    # 与 status 组合：只搜待审核里的命中
    resp = client.get(
        "/agent/market/reviews",
        params={"status": "pending", "keyword": "授信"},
        headers=HDR_USER,
    )
    assert [r["agent_id"] for r in resp.json()["reviews"]] == [hit1]


def test_review_detail_endpoint(client):
    """详情接口：发布表单字段 + 审核概况（reason 按状态区分）；404。"""
    aid = _create(client, HDR_ALICE, name="智文管理系统")
    # 无申请记录 → 404
    assert client.get(
        f"/agent/market/reviews/{aid}", headers=HDR_USER,
    ).status_code == 404

    # pending：表单字段齐全，审核概况为空
    assert client.post(
        f"/agent/market/{aid}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200
    resp = client.get(f"/agent/market/reviews/{aid}", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "智文管理系统"
    assert body["applicant"] == "alice"
    assert body["department"] == _PUB_BODY["department"]
    assert body["system_name"] == _PUB_BODY["system_name"]
    assert body["tag"] == _PUB_BODY["tag"]
    assert body["description"] == _PUB_BODY["description"]
    assert body["status"] == "pending"
    assert body["reason"] == ""
    assert body["reviewed_at"] is None
    assert body["created_at"] is not None

    # 驳回：审核概况带驳回理由（人人可查，无需 owner/审批人）
    _publish_and_decide(client, aid, HDR_ALICE, "reject", reason="缺少内容安全过滤")
    resp = client.get(f"/agent/market/reviews/{aid}", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "缺少内容安全过滤"
    assert body["reviewer"] == "reviewer-1"
    assert body["reviewed_at"] is not None


def test_approve_with_optional_reason(client):
    """通过时的审批意见选填：不传 = 空串；传了落库到 reason。"""
    aid1 = _create(client, HDR_ALICE, name="通过不带意见")
    _publish_and_decide(client, aid1, HDR_ALICE, "approve")
    detail = client.get(f"/agent/market/reviews/{aid1}", headers=HDR_USER).json()
    assert detail["status"] == "approved"
    assert detail["reason"] == ""

    aid2 = _create(client, HDR_ALICE, name="通过带意见")
    _publish_and_decide(client, aid2, HDR_ALICE, "approve", reason="同意上架")
    detail = client.get(f"/agent/market/reviews/{aid2}", headers=HDR_USER).json()
    assert detail["status"] == "approved"
    assert detail["reason"] == "同意上架"
    # 通过 = 上架：市场列表可见
    assert aid2 in {
        a["id"] for a in client.get("/agent/market").json()["agents"]
    }


def test_agent_list_carries_publish_status(client):
    """GET /agent/ 列表项带 publish_status（与 /agent/owned 同口径）。"""
    untouched = _create(client, HDR_ALICE, name="从未发布")
    published = _create(client, HDR_ALICE, name="走完整审批流")

    # 未发布 → not_submitted
    resp = client.get("/agent/", params={"pageSize": 50}, headers=HDR_ALICE)
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[untouched]["publish_status"] == "not_submitted"

    # pending
    assert client.post(
        f"/agent/market/{published}/publish", json=_PUB_BODY, headers=HDR_ALICE,
    ).status_code == 200
    resp = client.get("/agent/", params={"pageSize": 50}, headers=HDR_ALICE)
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[published]["publish_status"] == "pending"

    # approve → 已在市场 → approved
    _publish_and_decide(client, published, HDR_ALICE, "approve")
    resp = client.get("/agent/", params={"pageSize": 50}, headers=HDR_ALICE)
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[published]["publish_status"] == "approved"

    # 非 owner 下架（人人可操作）→ 回落 not_submitted（待发布）
    assert client.post(
        f"/agent/market/{published}/unpublish", headers=HDR_USER,
    ).status_code == 204
    resp = client.get("/agent/", params={"pageSize": 50}, headers=HDR_ALICE)
    by_id = {a["id"]: a for a in resp.json()["agents"]}
    assert by_id[published]["publish_status"] == "not_submitted"
