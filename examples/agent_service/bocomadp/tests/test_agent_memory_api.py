# -*- coding: utf-8 -*-
"""agent_api 模型 + 包裹路由测试。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from bocomadp.routers.agent_api import (
    CreateAgentRequestWithMemory,
    UpdateAgentRequestWithMemory,
)

# 集成测试所需（放顶部 import，避免在用例中部穿插 import 影响可读性）
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager

from bocomadp import memory_config
from bocomadp.routers.agent_api import install_agent_memory_router


class TestModels:
    def test_create_defaults(self):
        req = CreateAgentRequestWithMemory(name="a")
        assert req.memory_update_prompt == ""
        assert req.memory_enabled is False
        assert req.memory_type == 0
        assert req.memory_update_rounds == 10

    def test_create_invalid_memory_type(self):
        with pytest.raises(ValidationError):
            CreateAgentRequestWithMemory(name="a", memory_type=2)

    def test_create_invalid_rounds(self):
        with pytest.raises(ValidationError):
            CreateAgentRequestWithMemory(name="a", memory_update_rounds=-1)

    def test_update_memory_fields_optional(self):
        req = UpdateAgentRequestWithMemory(name="b")
        assert req.memory_update_prompt is None
        assert req.memory_enabled is None

    def test_update_clears_prompt_with_empty_string(self):
        req = UpdateAgentRequestWithMemory(memory_update_prompt="")
        assert req.memory_update_prompt == ""


# ------------------------------------------------------------------
# 集成测试：TestClient(create_app(...)) + sqlite + monkeypatch engine
# ------------------------------------------------------------------

HEADERS = {"X-User-ID": "test-user"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """完整 app（sqlite 存储 + sqlite 侧边存储）+ 包裹路由接管。"""
    # 侧边存储 engine → 临时文件 sqlite（:memory: 每连接独立库，不可用）
    import asyncio
    from sqlalchemy import text

    side_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'mem_side.db'}",
    )

    async def _init_side():
        async with side_engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS agent_memory_configs ("
                    "agent_id VARCHAR(255) PRIMARY KEY, "
                    "memory_update_prompt TEXT NOT NULL DEFAULT '', "
                    "memory_enabled BOOLEAN NOT NULL DEFAULT FALSE, "
                    "memory_type INTEGER NOT NULL DEFAULT 0, "
                    "memory_update_rounds INTEGER NOT NULL DEFAULT 10, "
                    "updated_at DATETIME NOT NULL"
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
    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(str(tmp_path / "ws")),
        enable_index_worker=False,
    )
    install_agent_memory_router(app)
    with TestClient(app) as test_client:
        yield test_client


class TestInstall:
    def test_framework_routes_removed(self, client):
        """安装后 OpenAPI 中 /agent/ 与 /agent/{agent_id} 各方法唯一。

        OpenAPI 中 ``paths`` 下每个路径的值是 ``{method: operation}``
        字典；断言路径下仅剩被包裹的方法（无重复条目）。
        """
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths["/agent/"].keys()) == {"get", "post"}
        assert set(paths["/agent/{agent_id}"].keys()) == {"patch", "delete"}

    def test_schema_v2_still_available(self, client):
        resp = client.get("/agent/schema/v2", headers=HEADERS)
        assert resp.status_code == 200


class TestCreate:
    def test_create_with_memory_fields(self, client):
        resp = client.post(
            "/agent/",
            json={
                "name": "mem-agent",
                "system_prompt": "hi",
                "memory_update_prompt": "记住用户偏好",
                "memory_enabled": True,
                "memory_type": 1,
                "memory_update_rounds": 3,
            },
            headers=HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["agent_id"]
        assert body["memory_update_prompt"] == "记住用户偏好"
        assert body["memory_enabled"] is True
        assert body["memory_type"] == 1
        assert body["memory_update_rounds"] == 3

    def test_create_defaults(self, client):
        resp = client.post(
            "/agent/",
            json={"name": "plain-agent"},
            headers=HEADERS,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["memory_update_prompt"] == ""
        assert body["memory_enabled"] is False
        assert body["memory_type"] == 0
        assert body["memory_update_rounds"] == 10

    def test_create_invalid_memory_type_422(self, client):
        resp = client.post(
            "/agent/",
            json={"name": "bad", "memory_type": 2},
            headers=HEADERS,
        )
        assert resp.status_code == 422

    def test_create_invalid_rounds_422(self, client):
        resp = client.post(
            "/agent/",
            json={"name": "bad", "memory_update_rounds": -1},
            headers=HEADERS,
        )
        assert resp.status_code == 422


class TestList:
    def test_list_merges_memory_fields(self, client):
        with_mem = client.post(
            "/agent/",
            json={"name": "a", "memory_enabled": True, "memory_type": 1},
            headers=HEADERS,
        ).json()
        no_mem = client.post("/agent/", json={"name": "b"}, headers=HEADERS).json()

        resp = client.get("/agent/", headers=HEADERS)
        assert resp.status_code == 200
        agents = {a["id"]: a for a in resp.json()["agents"]}
        assert agents[with_mem["agent_id"]]["memory_enabled"] is True
        assert agents[with_mem["agent_id"]]["memory_type"] == 1
        # 无记忆记录的智能体返回默认值
        assert agents[no_mem["agent_id"]]["memory_enabled"] is False
        assert agents[no_mem["agent_id"]]["memory_update_rounds"] == 10


class TestPatch:
    def test_patch_memory_only_keeps_core(self, client):
        created = client.post(
            "/agent/",
            json={"name": "a", "system_prompt": "p1"},
            headers=HEADERS,
        ).json()
        resp = client.patch(
            f"/agent/{created['agent_id']}",
            json={"memory_enabled": True},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["memory_enabled"] is True
        # 原字段未被改动：框架 AgentView 中 system_prompt 嵌套在 data 子对象
        assert body["data"]["system_prompt"] == "p1"

    def test_patch_none_keeps_memory(self, client):
        created = client.post(
            "/agent/",
            json={"name": "a", "memory_update_prompt": "keep-me"},
            headers=HEADERS,
        ).json()
        client.patch(
            f"/agent/{created['agent_id']}",
            json={"name": "renamed"},
            headers=HEADERS,
        )
        resp = client.patch(
            f"/agent/{created['agent_id']}",
            json={},
            headers=HEADERS,
        )
        assert resp.json()["memory_update_prompt"] == "keep-me"

    def test_patch_clears_prompt(self, client):
        created = client.post(
            "/agent/",
            json={"name": "a", "memory_update_prompt": "x"},
            headers=HEADERS,
        ).json()
        resp = client.patch(
            f"/agent/{created['agent_id']}",
            json={"memory_update_prompt": ""},
            headers=HEADERS,
        )
        assert resp.json()["memory_update_prompt"] == ""


class TestDelete:
    def test_delete_removes_agent_and_memory(self, client):
        created = client.post(
            "/agent/",
            json={"name": "a", "memory_enabled": True},
            headers=HEADERS,
        ).json()
        resp = client.delete(f"/agent/{created['agent_id']}", headers=HEADERS)
        assert resp.status_code == 204
        # 侧边记录已清理：查询列表不再出现该 agent，侧边直接读为 None
        listed = client.get("/agent/", headers=HEADERS).json()["agents"]
        assert all(a["id"] != created["agent_id"] for a in listed)

    def test_delete_missing_404(self, client):
        resp = client.delete("/agent/no-such-id", headers=HEADERS)
        assert resp.status_code == 404
