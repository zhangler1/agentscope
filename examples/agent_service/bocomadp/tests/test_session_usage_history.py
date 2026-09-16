# -*- coding: utf-8 -*-
"""用户使用视角接口集成测试（/sessions/usage/agents + /sessions/usage/history）。

覆盖（走真实 HTTP / 真 SQLite storage，模式与 test_agent_market 一致）：

1. GET /sessions/usage/agents —— 使用过的智能体清单：
   - sessions 按 agent_id 分组：自建 + 平台都在，排除 `_` 开头系统智能体；
   - 会话数 / 最近使用时间 / 智能体名 / is_platform / is_self 标记；
   - user_id 省略时回退 X-User-ID；
2. GET /sessions/usage/history —— 跨智能体历史会话：
   - 自建 + 平台混排，updated_at 倒序统一分页（COUNT + LIMIT/OFFSET）；
   - 每条附 agent_name（agents 表 payload["data"]["name"]）；
   - 会话名改写：默认时间名 → 首条用户输入 / 无输入 → "新对话"，
     用户自定义名不动（复用 /limit 的公共实现）；
   - 可选 agent_id 收窄到单个智能体。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与
test_agent_market.py 一致）。

跑法（仓库根目录）::

    venv\\Scripts\\python.exe -m pytest examples/agent_service/bocomadp/tests/test_session_usage_history.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import create_app
from agentscope.app._router._agent import (
    agent_router as _framework_agent_router,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
)
from agentscope.app.storage._sql._tables import MessageRow, SessionRow
from agentscope.app.workspace_manager import LocalWorkspaceManager

import bocomadp.pool_config as pool_config
from bocomadp import market_store
from bocomadp.routers.session_usage import session_usage_router

HDR_USER = {"X-User-ID": "test-user"}
HDR_ALICE = {"X-User-ID": "alice"}

# 固定时间轴：T0 < T1 < T2 < T3（分钟级错开，避免同刻排序不稳定）
_T0 = datetime(2026, 9, 15, 10, 0, 0)
_T1 = _T0 + timedelta(minutes=10)
_T2 = _T0 + timedelta(minutes=20)
_T3 = _T0 + timedelta(minutes=30)


def _remove_framework_agent_routes(app) -> None:
    """摘除框架内置 /agent 路由（main.py / test_agent_market 同款）。"""
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
    return AgentData(
        name=name,
        context_config=ContextConfig(),
        react_config=ReActConfig(),
    )


@pytest.fixture
def client(tmp_path):
    """sqlite 存储 + usage 路由；pool_config 单例引擎指向测试库。"""
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            # /limit 家族的裸 SQL 端点经 pool_config._get_engine() 拿连接，
            # 默认会连 config.yaml 的真库——测试里替换成测试存储的引擎。
            pool_config._engine = storage._engine
            # is_platform 按 agent_market 名单判定，建表备用
            await market_store.ensure_market_tables(storage)

    _run(_provision())

    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    _remove_framework_agent_routes(app)
    app.include_router(session_usage_router)
    with TestClient(app) as test_client:
        yield test_client

    # 单例复位，防止污染其他测试文件
    pool_config._engine = None


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------

_PLATFORM = "default"


def _seed_agents(client) -> None:
    """平台 2 个 + 自建 1 个 + 系统内置 1 个（校验排除逻辑）。"""
    storage = client.app.state.storage
    records = [
        AgentRecord(id="plat-a", user_id=_PLATFORM, data=_agent_data("汇率助手")),
        AgentRecord(id="plat-b", user_id=_PLATFORM, data=_agent_data("客服助手")),
        AgentRecord(id="own-a", user_id="test-user", data=_agent_data("周报生成器")),
        AgentRecord(id="_factory", user_id=_PLATFORM, data=_agent_data("内置工厂")),
    ]
    for rec in records:
        _run(storage.upsert_agent(rec.user_id, rec))

    # 平台智能体手动上架进市场名单（新口径：is_platform 按 agent_market
    # 行判定，user_id 不再承载"平台"语义）；_factory 是内部工具不上架。
    for aid in ("plat-a", "plat-b"):
        _run(market_store.insert_market_entry(storage, aid))


def _add_session(
    client,
    sid: str,
    user_id: str,
    agent_id: str,
    updated_at: datetime,
    *,
    name: str | None = None,
    first_user_msg: str | None = None,
    with_state: bool = False,
) -> None:
    """插一条会话；name=None 用框架默认时间名形态，first_user_msg 造首条输入。

    ``with_state=True`` 时 payload 里带一份 ``state.context``（模拟真实
    会话里塞满消息明细的形态），用于验证列表接口会把它裁掉。
    """
    storage = client.app.state.storage
    config = {
        "workspace_id": "w-fixed",
        "name": name or updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload: dict = {"config": config}
    if with_state:
        payload["state"] = {
            "session_id": sid,
            "summary": "",
            "context": [
                {
                    "name": "user",
                    "role": "user",
                    "content": [{"type": "text", "text": "历史消息明细"}],
                    "id": "msg-1",
                    "created_at": updated_at.isoformat(),
                    "metadata": {},
                    "usage": None,
                    "error": None,
                },
            ],
        }

    async def _go() -> None:
        async with storage._session_factory() as session:
            session.add(
                SessionRow(
                    id=sid,
                    user_id=user_id,
                    agent_id=agent_id,
                    source="user",
                    payload=payload,
                    created_at=updated_at,
                    updated_at=updated_at,
                ),
            )
            if first_user_msg is not None:
                session.add(
                    MessageRow(
                        session_id=sid,
                        msg_id="m0",
                        created_at=updated_at,
                        payload={"role": "user", "content": first_user_msg},
                    ),
                )
            await session.commit()

    _run(_go())


@pytest.fixture
def seeded(client):
    """test-user 的 5 条会话（4 条正常 + 1 条系统内置）+ alice 的 1 条。

    时间轴（新→旧）：s3(plat-a, T3) > s2(plat-a, T2) > s1(own-a, T1)
    > s4(plat-b, T0)；s5 挂在 _factory 上（应被排除）；alice 的会话
    不该出现在 test-user 的任何视角里。
    """
    _seed_agents(client)
    _add_session(
        client, "s1", "test-user", "own-a", _T1, name="周报会话",
    )
    _add_session(
        client, "s2", "test-user", "plat-a", _T2,
        first_user_msg="你好，今天汇率是多少",
    )
    _add_session(client, "s3", "test-user", "plat-a", _T3)
    _add_session(client, "s4", "test-user", "plat-b", _T0)
    _add_session(client, "s5", "test-user", "_factory", _T2)
    _add_session(client, "s-alice", "alice", "plat-a", _T3)
    return client


# ---------------------------------------------------------------------------
# 1) GET /sessions/usage/agents
# ---------------------------------------------------------------------------


def test_usage_agents_groups_and_flags(seeded):
    """分组聚合：自建+平台都在、系统内置排除、标记与计数正确。"""
    resp = seeded.get(
        "/sessions/usage/agents",
        params={"user_id": "test-user"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "test-user"
    assert body["total"] == 3  # plat-a / plat-b / own-a（_factory 被排除）

    by_id = {a["agent_id"]: a for a in body["agents"]}
    assert set(by_id) == {"plat-a", "plat-b", "own-a"}

    # 计数：plat-a 有 2 条会话
    assert by_id["plat-a"]["session_count"] == 2
    assert by_id["own-a"]["session_count"] == 1

    # 名称来自 agents 表 payload["data"]["name"]
    assert by_id["plat-a"]["name"] == "汇率助手"
    assert by_id["own-a"]["name"] == "周报生成器"

    # 平台/自建标记
    assert by_id["plat-a"]["is_platform"] is True
    assert by_id["plat-a"]["is_self"] is False
    assert by_id["own-a"]["is_platform"] is False
    assert by_id["own-a"]["is_self"] is True
    assert by_id["plat-a"]["owner_user_id"] == "default"

    # 按最近使用时间倒序：plat-a(T3) > own-a(T1) > plat-b(T0)
    assert [a["agent_id"] for a in body["agents"]] == [
        "plat-a", "own-a", "plat-b",
    ]


def test_usage_agents_falls_back_to_header(seeded):
    """user_id 省略 → 回退 X-User-ID。"""
    resp = seeded.get("/sessions/usage/agents", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "test-user"
    assert body["total"] == 3


def test_usage_agents_isolated_per_user(seeded):
    """alice 的视角：只有她自己的会话，与 test-user 互不可见。"""
    resp = seeded.get(
        "/sessions/usage/agents",
        params={"user_id": "alice"},
        headers=HDR_ALICE,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [a["agent_id"] for a in body["agents"]] == ["plat-a"]
    assert body["agents"][0]["session_count"] == 1


def test_usage_agents_empty_user(client):
    """从没用过的用户 → 空清单，200 而非报错。"""
    _seed_agents(client)
    resp = client.get(
        "/sessions/usage/agents",
        params={"user_id": "nobody"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    assert resp.json() == {"user_id": "nobody", "agents": [], "total": 0}


# ---------------------------------------------------------------------------
# 2) GET /sessions/usage/history
# ---------------------------------------------------------------------------


def test_usage_history_cross_agent_timeline(seeded):
    """跨智能体统一时间线：自建+平台混排，updated_at 倒序，附 agent_name。"""
    resp = seeded.get(
        "/sessions/usage/history",
        params={"user_id": "test-user"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "test-user"
    assert body["total"] == 4  # s1..s4（_factory 的 s5 被排除）

    ids = [s["id"] for s in body["sessions"]]
    assert ids == ["s3", "s2", "s1", "s4"]  # T3 > T2 > T1 > T0

    # 每条附智能体名
    by_id = {s["id"]: s for s in body["sessions"]}
    assert by_id["s2"]["agent_name"] == "汇率助手"
    assert by_id["s1"]["agent_name"] == "周报生成器"
    assert by_id["s4"]["agent_name"] == "客服助手"
    # 会话的 user_id/agent_id 原样返回
    assert by_id["s2"]["user_id"] == "test-user"
    assert by_id["s2"]["agent_id"] == "plat-a"
    assert body["has_more"] is False


def test_usage_history_title_rewrite(seeded):
    """会话名改写（复用 /limit 公共实现）：
    默认时间名 → 首条用户输入；无输入 → "新对话"；自定义名不动。"""
    resp = seeded.get(
        "/sessions/usage/history",
        params={"user_id": "test-user"},
        headers=HDR_USER,
    )
    by_id = {s["id"]: s for s in resp.json()["sessions"]}
    assert by_id["s2"]["config"]["name"] == "你好，今天汇率是多少"
    assert by_id["s3"]["config"]["name"] == "新对话"
    assert by_id["s1"]["config"]["name"] == "周报会话"  # 自定义名不动


def test_usage_history_pagination(seeded):
    """分页：LIMIT/OFFSET 推到 DB，has_more 正确。"""
    resp = seeded.get(
        "/sessions/usage/history",
        params={"user_id": "test-user", "page": 1, "page_size": 2},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert [s["id"] for s in body["sessions"]] == ["s3", "s2"]
    assert body["has_more"] is True

    resp = seeded.get(
        "/sessions/usage/history",
        params={"user_id": "test-user", "page": 2, "page_size": 2},
        headers=HDR_USER,
    )
    body = resp.json()
    assert [s["id"] for s in body["sessions"]] == ["s1", "s4"]
    assert body["has_more"] is False


def test_usage_history_agent_id_filter(seeded):
    """可选 agent_id 收窄到单个智能体（等价 /limit 但带 agent_name）。"""
    resp = seeded.get(
        "/sessions/usage/history",
        params={"user_id": "test-user", "agent_id": "plat-a"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [s["id"] for s in body["sessions"]] == ["s3", "s2"]
    assert all(s["agent_id"] == "plat-a" for s in body["sessions"])


def test_usage_history_falls_back_to_header(seeded):
    """user_id 省略 → 回退 X-User-ID。"""
    resp = seeded.get("/sessions/usage/history", headers=HDR_USER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "test-user"
    assert body["total"] == 4


def test_list_apis_omit_message_context(client):
    """列表接口不下发会话里的消息明细（state.context）。

    会话列表只负责"目录"（id/名字/时间），完整对话走
    GET /sessions/{id}/messages 分页取——消息明细随对话轮数线性膨胀，
    塞进列表响应会拖慢甚至超时。
    """
    _seed_agents(client)
    _add_session(
        client, "ctx-1", "test-user", "plat-a", _T0,
        name="带明细的会话", with_state=True,
    )

    # /sessions/limit（老接口）
    resp = client.get(
        "/sessions/limit", params={"agent_id": "plat-a"}, headers=HDR_USER,
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()["sessions"]
    assert [s["id"] for s in sessions] == ["ctx-1"]
    assert "context" not in (sessions[0].get("state") or {})
    # 名字照常改写/保留（裁 context 不影响目录信息）
    assert sessions[0]["config"]["name"] == "带明细的会话"

    # /sessions/usage/history（新接口）
    resp = client.get(
        "/sessions/usage/history",
        params={"user_id": "test-user"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200, resp.text
    for sess in resp.json()["sessions"]:
        assert "context" not in (sess.get("state") or {})
        assert sess["agent_name"]  # agent_name 照常附带


def test_usage_history_empty_user(client):
    """没用过的用户 → 空列表，200 而非报错。"""
    _seed_agents(client)
    resp = client.get(
        "/sessions/usage/history",
        params={"user_id": "nobody"},
        headers=HDR_USER,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"] == []
    assert body["total"] == 0
