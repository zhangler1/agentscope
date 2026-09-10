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


def test_config_shared_across_users(client):
    """配置按 agent_id 定位：u1 配置后，u2 也能读到同一份（agent 级）。"""
    r = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert r.status_code == 200
    r2 = client.get("/memory/config/agent1", headers={"X-User-ID": "u2"})
    assert r2.status_code == 200
    assert r2.json()["memory_enabled"] is True


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


def test_put_disabled_does_not_register(client, monkeypatch):
    """memory_enabled=false 不触发平台注册，caller 保持为空。"""
    calls = []
    import bocomadp.memory.platform as pf

    async def _reg(p):
        calls.append(p)
        return "c1"

    monkeypatch.setattr(pf, "register_agent", _reg)
    r = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": False},
        headers={"X-User-ID": "u1"},
    )
    assert r.status_code == 200
    assert calls == []  # 禁用状态不注册
    assert r.json()["caller"] == ""  # caller 未落库


def test_global_config_default_memory_prompt_roundtrip(tmp_path, monkeypatch):
    """default_memory_prompt 为显式声明字段：可经全局配置存库并可读回。"""
    import bocomadp.runtime_config_store as rcs
    from bocomadp.memory.config import MemoryRuntimeConfig

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rc.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(rcs, "_get_engine", fake_engine)

    async def _roundtrip():
        rt = MemoryRuntimeConfig(
            idle_minutes=20,
            default_memory_prompt="全局默认提示词",
        )
        await rcs.config_set("memory", rt.model_dump())
        return await rcs.config_get("memory")

    got = asyncio.run(_roundtrip())
    assert got["idle_minutes"] == 20
    assert got["default_memory_prompt"] == "全局默认提示词"
    # 默认值：未传时为 ""
    assert MemoryRuntimeConfig().default_memory_prompt == ""


def test_global_config_ignores_undeclared_fields(tmp_path, monkeypatch):
    """未开启 extra=allow：未声明字段被 pydantic 丢弃（不落库）。"""
    import bocomadp.runtime_config_store as rcs
    from bocomadp.memory.config import MemoryRuntimeConfig

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rc.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(rcs, "_get_engine", fake_engine)

    async def _roundtrip():
        rt = MemoryRuntimeConfig(idle_minutes=20, **{"custom_extra": "x"})
        await rcs.config_set("memory", rt.model_dump())
        return await rcs.config_get("memory")

    got = asyncio.run(_roundtrip())
    assert got["idle_minutes"] == 20
    assert "custom_extra" not in got  # 未声明字段不保留


def test_enable_after_disabled_registers_once(client, monkeypatch):
    """先 false（不注册）→ 后置 true（补注册一次），false 阶段不产生 caller。"""
    calls = []
    import bocomadp.memory.platform as pf

    async def _reg(p):
        calls.append(p)
        return "c1"

    monkeypatch.setattr(pf, "register_agent", _reg)
    r1 = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": False},
        headers={"X-User-ID": "u1"},
    )
    assert calls == [] and r1.json()["caller"] == ""
    r2 = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert len(calls) == 1  # 置 true 时补注册
    assert r2.json()["caller"] == "c1"
