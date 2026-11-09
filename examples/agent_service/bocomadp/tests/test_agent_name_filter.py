# -*- coding: utf-8 -*-
"""GET /agent/ 按智能体名称模糊查询（``name``）回归测试。

名称在 ``data.name``（列表项顶层没有 name）：普通用户视图的 data 是模型、
管理员全量视图的 data 来自 payload JSON，两种形态都要能过滤。
pytest-asyncio 未安装，异步逻辑用 asyncio.run 包裹。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agentscope.app import create_app
from agentscope.app._router._agent import (
    agent_router as _framework_agent_router,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import (
    chat_concurrency_config,
    market_review_store,
    market_store,
    role_store,
    team_store,
)
from bocomadp.routers.agent import agent_router

HDR = {"X-User-ID": "alice"}


def _run(coro):
    return asyncio.run(coro)


def _remove_framework_agent_routes(app) -> None:
    """摘除框架内置 /agent 路由（与 main.py / 既有集成测试同款）。"""
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


def _create(client, name: str) -> str:
    resp = client.post("/agent/", json={"name": name}, headers=HDR)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


def _get(client, **params):
    resp = client.get("/agent/", headers=HDR, params=params or None)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _names(body) -> list[str]:
    return [a["data"]["name"] for a in body["agents"]]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离角色与并发配置真源，测试只盯名称过滤，不连真实业务库。"""
    async def _false(user_id, role_key):
        return False

    async def _empty_configs():
        return {}

    monkeypatch.setattr(role_store, "has_role", _false)
    monkeypatch.setattr(chat_concurrency_config, "pg_list_all", _empty_configs)


@pytest.fixture
def env(tmp_path):
    """SQLite 存储 + bocomadp 三张表 + 列表路由，返回 (client, storage)。"""
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'agents.db'}",
        create_tables=True,
    )

    async def _provision():
        async with storage:
            await team_store.ensure_team_tables(storage)
            await market_store.ensure_market_tables(storage)
            await market_review_store.ensure_review_tables(storage)

    _run(_provision())
    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    _remove_framework_agent_routes(app)
    app.include_router(agent_router)
    with TestClient(app) as client:
        yield client, storage


def test_substring_and_case_insensitive(env):
    """子串命中、中文、忽略大小写；查无结果返回空列表而不是报错。"""
    client, _storage = env
    for name in ("周报生成器", "月报生成器", "Alpha 助手", "beta 工具"):
        _create(client, name)

    body = _get(client, name="报")
    assert body["total"] == 2, body
    assert set(_names(body)) == {"周报生成器", "月报生成器"}

    body = _get(client, name="周报")
    assert body["total"] == 1 and _names(body) == ["周报生成器"]

    body = _get(client, name="ALPHA")
    assert body["total"] == 1 and _names(body) == ["Alpha 助手"]

    body = _get(client, name="BETA")
    assert body["total"] == 1 and _names(body) == ["beta 工具"]

    body = _get(client, name="查无此名")
    assert body["total"] == 0 and body["agents"] == []


def test_blank_name_means_no_filter(env):
    """不传 / 空串 / 纯空白都不过滤，与既有列表行为一致。"""
    client, _storage = env
    for name in ("甲", "乙", "丙"):
        _create(client, name)

    assert _get(client)["total"] == 3
    assert _get(client, name="")["total"] == 3
    assert _get(client, name="   ")["total"] == 3
    assert _get(client, name="甲")["total"] == 1


def test_total_and_paging_use_filtered_result(env):
    """total 是过滤后总数，分页只切过滤结果。"""
    client, _storage = env
    for name in ("目标-1", "目标-2", "目标-3", "其它-1", "其它-2"):
        _create(client, name)

    first = _get(client, name="目标", pageNum=1, pageSize=2)
    assert first["total"] == 3, first
    assert len(first["agents"]) == 2
    assert all(n.startswith("目标") for n in _names(first))

    second = _get(client, name="目标", pageNum=2, pageSize=2)
    assert second["total"] == 3, second
    assert len(second["agents"]) == 1
    assert _names(second)[0].startswith("目标")


def test_reviewer_full_view_filters_payload_name(env, monkeypatch):
    """管理员全量视图（data 来自 payload）同样能按名称过滤。"""
    client, _storage = env
    _create(client, "命中-管理员")
    for name in ("其它-管理员", "另一个"):
        _create(client, name)

    async def _true(user_id, role_key):
        return True

    monkeypatch.setattr(role_store, "has_role", _true)

    body = _get(client, name="命中")
    assert body["total"] == 1, body
    assert _names(body) == ["命中-管理员"]
    assert "concurrency_limit" in body["agents"][0]


def test_existing_fields_unchanged_without_name(env):
    """不传 name 时既有字段（含上一需求的并发路数）原样保留。"""
    client, _storage = env
    _create(client, "回归检查")

    body = _get(client)
    assert body["total"] == 1
    item = body["agents"][0]
    assert item["data"]["name"] == "回归检查"
    assert item["publish_status"] == "not_submitted"
    assert item["concurrency_limit"] == 1
