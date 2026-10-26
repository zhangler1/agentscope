# -*- coding: utf-8 -*-
"""``memory.agent_memory_cleanup``：删除 agent 后清理记忆的中间件与挂载。"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

import bocomadp.memory as memory
from bocomadp.memory.agent_memory_cleanup import AgentMemoryCleanupMiddleware


@pytest.fixture
def cleanup_calls(monkeypatch):
    """替换 ``cleanup_agent_memory``，只记录被清理的 agent_id。"""
    calls: list[str] = []

    async def _fake(agent_id: str) -> None:
        calls.append(agent_id)

    monkeypatch.setattr(memory, "cleanup_agent_memory", _fake)
    return calls


def _client() -> TestClient:
    def delete_agent(request):
        if request.path_params["agent_id"] == "missing":
            return JSONResponse({"detail": "not found"}, status_code=404)
        return Response(status_code=204)

    def delete_other(request):
        return Response(status_code=204)

    app = Starlette(
        routes=[
            Route("/agent/{agent_id}", delete_agent, methods=["DELETE"]),
            Route("/other/{item_id}", delete_other, methods=["DELETE"]),
        ],
    )
    app.add_middleware(AgentMemoryCleanupMiddleware)
    return TestClient(app)


def test_cleanup_after_successful_agent_delete(cleanup_calls):
    response = _client().delete("/agent/agent-1")

    assert response.status_code == 204
    assert cleanup_calls == ["agent-1"]


def test_no_cleanup_when_delete_failed(cleanup_calls):
    response = _client().delete("/agent/missing")

    assert response.status_code == 404
    assert cleanup_calls == []


def test_no_cleanup_for_other_delete_routes(cleanup_calls):
    response = _client().delete("/other/x")

    assert response.status_code == 204
    assert cleanup_calls == []


def test_install_memory_mounts_cleanup_middleware():
    from fastapi import FastAPI

    app = FastAPI()
    memory.install_memory(app)

    assert any(
        middleware.cls is AgentMemoryCleanupMiddleware
        for middleware in app.user_middleware
    )


def test_install_memory_can_disable_cleanup_middleware():
    from fastapi import FastAPI

    app = FastAPI()
    memory.install_memory(app, cleanup_on_agent_delete=False)

    assert not any(
        middleware.cls is AgentMemoryCleanupMiddleware
        for middleware in app.user_middleware
    )
