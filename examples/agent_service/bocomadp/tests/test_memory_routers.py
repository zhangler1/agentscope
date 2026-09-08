# -*- coding: utf-8 -*-
"""memory/routers.py 配置 API 单测：SQLite engine + monkeypatch 平台注册。

路径说明：memory_router prefix=``/memory/config``，直接 include 到测试 app；
生产 main.py 把 app 挂到 ``/api`` 前缀下，对外即 ``/api/memory/config[...]``。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.memory import routers
from bocomadp.memory import store as memory_store


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = FastAPI()
    routers.install(app)
    # 用 SQLite engine 替换 store 的真实 PG engine
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(memory_store, "_get_engine", fake_engine)
    # 拦截平台注册：返回固定 caller
    import bocomadp.memory.platform as pf

    async def _reg(p):
        return "caller-1"

    monkeypatch.setattr(pf, "register_agent", _reg)
    return TestClient(app)


def test_put_then_get(client):
    r = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert r.status_code == 200
    r = client.get("/memory/config/agent1", headers={"X-User-ID": "u1"})
    assert r.status_code == 200 and r.json()["memory_enabled"] is True
    assert r.json()["caller"] == "caller-1"


def test_get_404_when_missing(client):
    r = client.get("/memory/config/nope", headers={"X-User-ID": "u1"})
    assert r.status_code == 404


def test_delete(client):
    client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert (
        client.delete("/memory/config/agent1", headers={"X-User-ID": "u1"}).status_code
        == 204
    )
    assert (
        client.get("/memory/config/agent1", headers={"X-User-ID": "u1"}).status_code
        == 404
    )


def test_put_second_time_no_repeat_register(client, monkeypatch):
    calls = []
    import bocomadp.memory.platform as pf

    async def _reg(p):
        calls.append(p)
        return "caller-1"

    monkeypatch.setattr(pf, "register_agent", _reg)
    client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    client.put(
        "/memory/config/agent1",
        json={"top_k": 8},
        headers={"X-User-ID": "u1"},
    )
    assert len(calls) == 1  # 已有 caller 不再注册
