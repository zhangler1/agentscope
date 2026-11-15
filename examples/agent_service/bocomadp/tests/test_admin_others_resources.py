# -*- coding: utf-8 -*-
"""管理员（reviewer）可看 / 管他人名下的「工具」与「知识库」资源：真机集成测试。

与智能体改 / 删同构：reviewer 不再被框架按 viewer 隔离的解析卡成 404，而是
按 agent_id 直查真实 owner 后以 owner 身份操作。本文件验证三件事：

1. ``reviewer_resolve_owner_id`` 公用工具：reviewer 查别人 → 真实 owner；
   普通用户 → 返回自身（不查库）。
2. ``GET /agents/{id}/tools``：reviewer 看别人的 → 200；普通用户 → 404。
3. ``GET/PUT /agents/{id}/cross-search-config``、
   ``GET /agents/{id}/cross-search-refs``：reviewer 以真实 owner 作主键查询
   （monkeypatch 掉 MySQL 后端避免依赖外部库），普通用户仍 404。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app import create_app
from agentscope.app._router._agent import agent_router as _framework_agent_router
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AgentData, AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import market_store, role_store, team_store, market_review_store
from bocomadp import agent_cross_search_config as config_mod
from bocomadp import agent_cross_search_ref as ref_mod
from bocomadp.routers.agent import agent_router
from bocomadp.routers.market import market_router
from bocomadp.routers.agent_tools import agent_tools_router
from bocomadp.routers import agent_cross_search_config as config_router_mod
from bocomadp.routers import agent_cross_search_refs as refs_router_mod
from bocomadp.reviewer_access import reviewer_resolve_owner_id
from bocomadp.memory import config_store as _memory_store
from bocomadp.memory.settings import configure_settings, MemorySettings
from bocomadp.memory.routers import memory_router
from bocomadp.routers import skill_router
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.app.storage._model._session import SessionConfig
from fastapi import HTTPException

import tempfile

HDR_USER = {"X-User-ID": "test-user"}
HDR_ALICE = {"X-User-ID": "alice"}
HDR_REVIEWER = {"X-User-ID": "reviewer-1"}


def _run(coro):
    return asyncio.run(coro)


def _remove_framework_agent_routes(app) -> None:
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
def env(tmp_path):
    """sqlite storage + 全部相关 router + reviewer-1 角色。"""
    storage = AsyncSQLAlchemyStorage(
        f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
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
    app.include_router(agent_tools_router)
    app.include_router(config_router_mod.agent_cross_search_config_router)
    app.include_router(refs_router_mod.agent_cross_search_refs_router)
    # 记忆配置端点：注入部署配置 + 用内存桩接管读写（避免依赖真实 SQL 后端）。
    configure_settings(MemorySettings(environment="sit"))
    _memory_store._storage = storage
    _mem_records: dict = {}

    async def _fake_get(agent_id: str):
        return _mem_records.get(agent_id)

    async def _fake_upsert(record):
        _mem_records[record.agent_id] = record
        return record

    async def _fake_delete(agent_id: str):
        _mem_records.pop(agent_id, None)
        return True

    _memory_store.get_memory_config = _fake_get
    _memory_store.upsert_memory_config = _fake_upsert
    _memory_store.delete_memory_config = _fake_delete
    app.include_router(memory_router)
    # 工具列表端点需要 app.state.tool_registry；用空桩避免依赖真实注册表。
    app.state.tool_registry = SimpleNamespace(list_tools=lambda: [])

    with TestClient(app) as client:
        yield {"client": client, "storage": storage}

    _run(role_store.revoke_role("reviewer-1", role_store.REVIEWER_ROLE))


def _create(client, headers, name: str = "x") -> str:
    resp = client.post("/agent/", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["agent_id"]


def test_reviewer_resolve_owner_id_returns_true_owner(env):
    client = env["client"]
    storage = env["storage"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    # reviewer 查 alice 的智能体 → 真实 owner = alice
    owner = _run(reviewer_resolve_owner_id(storage, "reviewer-1", alice_agent))
    assert owner == "alice"

    # 普通用户 → 返回自身（不查库，由后续 get_agent 校验）
    owner_self = _run(reviewer_resolve_owner_id(storage, "test-user", alice_agent))
    assert owner_self == "test-user"


def test_reviewer_can_list_others_tools(env):
    client = env["client"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    # 普通用户看 alice 的 → 404（原行为不变）
    assert (
        client.get(f"/agents/{alice_agent}/tools", headers=HDR_USER).status_code
        == 404
    )
    # reviewer 看 alice 的 → 200
    resp = client.get(f"/agents/{alice_agent}/tools", headers=HDR_REVIEWER)
    assert resp.status_code == 200, resp.text
    # alice 自己看 → 200
    assert (
        client.get(f"/agents/{alice_agent}/tools", headers=HDR_ALICE).status_code
        == 200
    )


def test_reviewer_sees_others_cross_search_config(env, monkeypatch):
    client = env["client"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    captured = {}
    real_config = config_mod.AgentCrossSearchConfig(user_code="captured")

    async def fake_get(user_id, agent_id):
        captured["user_id"] = user_id
        return real_config

    monkeypatch.setattr(
        config_router_mod, "cross_search_config_get", fake_get,
    )

    # reviewer 看 alice 的 → 以 owner=alice 查询，200
    resp = client.get(
        f"/agents/{alice_agent}/cross-search-config", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    assert captured["user_id"] == "alice"

    # 普通用户看 alice 的 → 404（看不到别人的）
    assert (
        client.get(
            f"/agents/{alice_agent}/cross-search-config", headers=HDR_USER,
        ).status_code
        == 404
    )

    # alice 自己看 → 仍以 owner=alice 查询
    captured.clear()
    resp = client.get(
        f"/agents/{alice_agent}/cross-search-config", headers=HDR_ALICE,
    )
    assert resp.status_code == 200
    assert captured["user_id"] == "alice"


def test_reviewer_sets_others_cross_search_config(env, monkeypatch, caplog):
    client = env["client"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    captured = {}

    async def fake_upsert(user_id, agent_id, config):
        captured["user_id"] = user_id

    monkeypatch.setattr(
        config_router_mod, "cross_search_config_upsert", fake_upsert,
    )

    body = {
        "user_code": "U001",
        "search_type": "0",
        "space_code_list": [],
        "team_space_code_list": [],
        "psnl_space_code_id": "",
        "psnl_category_id_list": [],
        "customized_tag_list": [],
        "text_top_n": None,
        "vector_top_n": None,
    }
    with caplog.at_level("INFO", logger="bocomadp.market_audit"):
        resp = client.put(
            f"/agents/{alice_agent}/cross-search-config",
            json=body,
            headers=HDR_REVIEWER,
        )
    assert resp.status_code == 200, resp.text
    assert captured["user_id"] == "alice"  # 以真实 owner 写回
    assert any(
        "cross_search_config_set_by_admin" in r.message for r in caplog.records
    )


def test_reviewer_sees_others_cross_search_refs(env, monkeypatch):
    client = env["client"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    captured = {}

    class _FakeItem:
        owner_user_code = "o"
        space_code = "s"
        space_name = "n"
        created_at = datetime(2026, 1, 1, 0, 0, 0)

    async def fake_list(user_id, agent_id):
        captured["user_id"] = user_id
        return [_FakeItem()]

    monkeypatch.setattr(refs_router_mod, "cross_search_ref_list", fake_list)

    # reviewer 看 alice 的 → 以 owner=alice 查询，200
    resp = client.get(
        f"/agents/{alice_agent}/cross-search-refs", headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    assert captured["user_id"] == "alice"

    # 普通用户看 alice 的 → 404
    assert (
        client.get(
            f"/agents/{alice_agent}/cross-search-refs", headers=HDR_USER,
        ).status_code
        == 404
    )


def test_reviewer_can_read_write_others_memory(env):
    client = env["client"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    body = {
        "injection_enabled": True,
        "extraction_enabled": False,
        "memory_prompt": "remember this",
    }
    # 普通用户看 alice 的 → 404（原行为不变，看不到别人的）
    assert (
        client.get(f"/memory/config/{alice_agent}", headers=HDR_USER).status_code
        == 404
    )
    # 普通用户写 alice 的 → 404（原行为不变）
    assert (
        client.put(
            f"/memory/config/{alice_agent}", json=body, headers=HDR_USER,
        ).status_code
        == 404
    )
    # reviewer 写 alice 的 → 200（以真实 owner=alice 落库，平台注册失败仅记错）
    resp = client.put(
        f"/memory/config/{alice_agent}", json=body, headers=HDR_REVIEWER,
    )
    assert resp.status_code == 200, resp.text
    # reviewer 读回 → 200
    resp = client.get(f"/memory/config/{alice_agent}", headers=HDR_REVIEWER)
    assert resp.status_code == 200, resp.text
    # alice 自己能读到 reviewer 写入的值
    resp = client.get(f"/memory/config/{alice_agent}", headers=HDR_ALICE)
    assert resp.status_code == 200
    assert resp.json()["memory_prompt"] == "remember this"


def test_reviewer_resolves_owner_session_for_skills(env, monkeypatch):
    client = env["client"]
    storage = env["storage"]
    alice_agent = _create(client, HDR_ALICE, name="alice-agent")

    # 会话归属 alice；reviewer 名下没有该会话 → 需借用 alice 身份解析。
    async def fake_get_session(user_id, agent_id, session_id):
        if user_id == "alice":
            return SimpleNamespace(config=SimpleNamespace(workspace_id="ws-alice"))
        return None

    monkeypatch.setattr(storage, "get_session", fake_get_session)

    ws_manager = LocalWorkspaceManager(tempfile.mkdtemp())
    # reviewer 借用 alice 身份 → 能解析到会话 workspace
    ws = _run(
        skill_router._resolve_workspace(
            "reviewer-1", alice_agent, "sid", storage, ws_manager,
        )
    )
    assert ws is not None
    # 普通用户（非 alice）按自身身份查 → 会话不存在 → 404
    with pytest.raises(HTTPException) as exc:
        _run(
            skill_router._resolve_workspace(
                "test-user", alice_agent, "sid", storage, ws_manager,
            )
        )
    assert exc.value.status_code == 404
