# -*- coding: utf-8 -*-
"""GET /agent/ 列表项 ``concurrency_limit``（生效并发路数）回归测试。

口径与闸机 ``effective_limit`` 一致：未过审 = 1 / 过审无配置 = 1 /
过审有配置 = 配置值（显式 0 原样）/ 配置查询失败 = 1（fail-closed）。
名单行（``agent_market``）= 审核通过的唯一凭证；配置真源用替身注入，
测试不连真实业务库。pytest-asyncio 未安装，异步逻辑用 asyncio.run 包裹。
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


@pytest.fixture(autouse=True)
def _plain_user(monkeypatch):
    """把角色查询隔离掉：本组用例只盯列表字段，不测管理员可见性。"""
    async def _false(user_id, role_key):
        return False

    monkeypatch.setattr(role_store, "has_role", _false)


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


def _mark_approved(storage, *agent_ids: str) -> None:
    """写市场名单行 = 审核通过（与 approve 上架同一凭证）。"""
    async def _go():
        for aid in agent_ids:
            await market_store.insert_market_entry(storage, aid)

    _run(_go())


def _set_configs(monkeypatch, configs: dict[str, int]) -> None:
    """替身 PG 全量配置，避免测试连真实业务库。"""
    async def _fake_pg_list_all():
        return {aid: (size, 0.0, "") for aid, size in configs.items()}

    monkeypatch.setattr(
        chat_concurrency_config, "pg_list_all", _fake_pg_list_all,
    )


def _by_id(client, params=None) -> dict[str, dict]:
    resp = client.get("/agent/", headers=HDR, params=params)
    assert resp.status_code == 200, resp.text
    return {a["id"]: a for a in resp.json()["agents"]}


def test_four_cases(env, monkeypatch):
    """四种口径：过审配 5 / 过审配 0 / 过审无配置 / 未过审有残留配置。"""
    client, storage = env
    configured = _create(client, "过审配5路")
    unlimited = _create(client, "过审配0路")
    no_config = _create(client, "过审无配置")
    unapproved = _create(client, "未过审但有残留配置")

    _mark_approved(storage, configured, unlimited, no_config)
    _set_configs(
        monkeypatch,
        {configured: 5, unlimited: 0, unapproved: 9},
    )

    views = _by_id(client)
    assert views[configured]["concurrency_limit"] == 5
    assert views[unlimited]["concurrency_limit"] == 0
    assert views[no_config]["concurrency_limit"] == 1
    assert views[unapproved]["concurrency_limit"] == 1
    # 发布/审批状态是既有字段，本次纯新增不能把它改坏
    assert views[configured]["publish_status"] == "approved"
    assert views[unapproved]["publish_status"] == "not_submitted"


def test_paged_only_current_page(env, monkeypatch):
    """分页：只给当前页批量算值，total 仍是分页前总数。"""
    client, storage = env
    ids = [_create(client, f"agent-{i}") for i in range(3)]
    _mark_approved(storage, *ids)
    _set_configs(monkeypatch, {aid: 3 for aid in ids})

    seen: list[list[str]] = []
    original = chat_concurrency_config.batch_effective_limits

    async def _spy(agent_ids, approved_ids):
        seen.append(list(agent_ids))
        return await original(agent_ids, approved_ids)

    monkeypatch.setattr(
        chat_concurrency_config, "batch_effective_limits", _spy,
    )
    resp = client.get(
        "/agent/", headers=HDR, params={"pageNum": 1, "pageSize": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert len(body["agents"]) == 2
    assert all(a["concurrency_limit"] == 3 for a in body["agents"])
    assert len(seen) == 1 and len(seen[0]) == 2


def test_batch_query_failure_falls_back_to_1(env, monkeypatch):
    """批量查询抛异常：列表仍 200，整批回落 1，不把接口拖成 500。"""
    client, storage = env
    aid = _create(client, "过审配5路")
    _mark_approved(storage, aid)
    _set_configs(monkeypatch, {aid: 5})

    async def _boom(agent_ids, approved_ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        chat_concurrency_config, "batch_effective_limits", _boom,
    )
    assert _by_id(client)[aid]["concurrency_limit"] == 1


def test_empty_config_table_falls_back_to_1(env, monkeypatch):
    """配置真源查不到（PG 故障回落空字典）：过审也按 1 路 fail-closed。"""
    client, storage = env
    aid = _create(client, "过审配5路")
    _mark_approved(storage, aid)

    async def _empty():
        return {}

    monkeypatch.setattr(chat_concurrency_config, "pg_list_all", _empty)
    assert _by_id(client)[aid]["concurrency_limit"] == 1


def test_owned_keeps_old_contract(env, monkeypatch):
    """GET /agent/owned 不改：响应里不出现 concurrency_limit。"""
    client, storage = env
    aid = _create(client, "过审配7路")
    _mark_approved(storage, aid)
    _set_configs(monkeypatch, {aid: 7})

    resp = client.get("/agent/owned", headers=HDR)
    assert resp.status_code == 200, resp.text
    items = resp.json()["agents"]
    assert items
    assert all("concurrency_limit" not in it for it in items)


def test_batch_effective_limits_rules(monkeypatch):
    """纯函数层：一次算出四种口径，0 原样、未过审不看配置。"""
    async def _fake_pg_list_all():
        return {
            "a": (5, 0.0, ""),
            "b": (0, 0.0, ""),
            "d": (9, 0.0, ""),
        }

    monkeypatch.setattr(
        chat_concurrency_config, "pg_list_all", _fake_pg_list_all,
    )
    result = _run(
        chat_concurrency_config.batch_effective_limits(
            ["a", "b", "c", "d"], {"a", "b", "c"},
        ),
    )
    assert result == {"a": 5, "b": 0, "c": 1, "d": 1}
