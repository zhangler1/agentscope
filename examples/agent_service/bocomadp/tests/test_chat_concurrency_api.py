# -*- coding: utf-8 -*-
"""智能体 /chat 并发上限管理接口测试（PUT/GET列表分页/GET单条/DELETE）。

轻量装配：FastAPI 直挂 ``agent_chat_concurrency_router``，PG 读写与
运行时热层函数全部 monkeypatch（不碰真库），storage/guard 用桩对象；
依赖 ``get_current_user_id`` 只读 ``X-User-ID`` header 的特性。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bocomadp import chat_concurrency_config
from bocomadp.routers.agent_chat_concurrency import (
    agent_chat_concurrency_router,
)

HDR = {"X-User-ID": "admin"}

# 桩 storage 里的智能体名册（agent_id → name）
_AGENTS = {
    "ag-1": "数据分析助手",
    "ag-2": "周报生成器",
    "ag-3": "翻译官",
}


class _FakeStorage:
    async def get_agent(self, user_id: str, agent_id: str):
        if agent_id in _AGENTS:
            return SimpleNamespace(name=_AGENTS[agent_id])
        return None


@pytest.fixture
def env(monkeypatch):
    """内存桩：PG 表 db、运行时缓存 cache、has_any 标志盒。"""
    db: dict[str, tuple[int, float]] = {}
    cache: dict[str, int] = {}
    has_any = {"v": False}

    async def pg_upsert(agent_id: str, max_running: int, agent_name="") -> None:
        db[agent_id] = (max_running, time.time(), agent_name)

    async def pg_get_full(agent_id: str):
        return db.get(agent_id)

    async def pg_delete(agent_id: str) -> None:
        db.pop(agent_id, None)

    async def pg_list_all() -> dict[str, tuple[int, float, str]]:
        return dict(db)

    async def pg_count() -> int:
        return len(db)

    async def pg_list_filtered(agent_name, page_num, page_size):
        rows = []
        for aid, (size, ts, name) in db.items():
            if agent_name and agent_name not in (name or ""):
                continue
            rows.append((aid, size, ts, name))
        rows.sort(key=lambda r: (-r[2], r[0]))
        total = len(rows)
        start = (page_num - 1) * page_size
        return rows[start:start + page_size], total

    async def pg_agent_names(ids: list[str]) -> dict[str, str]:
        return {i: _AGENTS[i] for i in ids if i in _AGENTS}

    async def set_runtime(agent_id: str, max_running: int) -> bool:
        cache[agent_id] = max_running
        return True

    async def delete_runtime(agent_id: str) -> None:
        cache.pop(agent_id, None)

    async def get_effective(agent_id: str):
        # 与真实模块同解析链：缓存(运行时层) → PG → default
        if agent_id in cache:
            return cache[agent_id], "pg"
        if agent_id in db:
            return db[agent_id][0], "pg"
        return None, "default"

    for name, fn in [
        ("pg_upsert", pg_upsert),
        ("pg_get_full", pg_get_full),
        ("pg_delete", pg_delete),
        ("pg_list_all", pg_list_all),
        ("pg_list_filtered", pg_list_filtered),
        ("pg_count", pg_count),
        ("pg_agent_names", pg_agent_names),
        ("set_runtime", set_runtime),
        ("delete_runtime", delete_runtime),
        ("get_effective", get_effective),
        ("mark_has_any", lambda v: has_any.__setitem__("v", v)),
    ]:
        monkeypatch.setattr(chat_concurrency_config, name, fn)

    app = FastAPI()
    app.include_router(agent_chat_concurrency_router)
    app.state.storage = _FakeStorage()
    with TestClient(app) as test_client:
        yield test_client, db, cache, has_any


def _run(coro):
    return asyncio.run(coro)


# ── PUT 设置上限 ─────────────────────────────────────────────────


def test_put_sets_limit(env):
    client, db, cache, has_any = env
    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-1", "max_running": 5},
        headers=HDR,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "agent_id": "ag-1",
        "max_running": 5,
        "runtime_synced": True,
    }
    assert db["ag-1"][0] == 5
    assert cache["ag-1"] == 5
    assert has_any["v"] is True


def test_put_allows_zero_as_explicit_unlimited(env):
    client, db, cache, has_any = env
    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-2", "max_running": 0},
        headers=HDR,
    )
    assert resp.status_code == 200
    assert resp.json()["max_running"] == 0
    assert cache["ag-2"] == 0


def test_put_agent_not_found(env):
    client, *_ = env
    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ghost", "max_running": 5},
        headers=HDR,
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found"}


def test_put_validation_errors(env):
    client, *_ = env
    # max_running 负数
    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-1", "max_running": -1},
        headers=HDR,
    )
    assert resp.status_code == 422
    # 缺 agent_id
    resp = client.put(
        "/agents/chat-concurrency",
        json={"max_running": 5},
        headers=HDR,
    )
    assert resp.status_code == 422


# ── GET 全量分页列表（agent_name 可选：空=全部，非空=按名称模糊） ──────────


def _seed_three(db: dict) -> None:
    """三条配置，updated_at 各不相同（验证倒序）。"""
    now = time.time()
    db["ag-1"] = (5, now, _AGENTS["ag-1"])
    db["ag-2"] = (0, now - 60, _AGENTS["ag-2"])
    db["ag-3"] = (2, now - 120, _AGENTS["ag-3"])


def test_list_pagination_and_order(env):
    client, db, _, _ = env
    _seed_three(db)
    resp = client.get(
        "/agents/chat-concurrency",
        params={"pageNum": 1, "pageSize": 2},
        headers=HDR,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["page_num"] == 1
    assert body["page_size"] == 2
    # 最近设置的在前
    assert [it["agent_id"] for it in body["items"]] == ["ag-1", "ag-2"]
    first = body["items"][0]
    assert first == {
        "agent_id": "ag-1",
        "agent_name": "数据分析助手",
        "max_running": 5,
        "source": "pg",
        "updated_at": first["updated_at"],
    }
    assert "current_running" not in first  # 列表只返回配置信息

    # 第二页
    resp = client.get(
        "/agents/chat-concurrency",
        params={"pageNum": 2, "pageSize": 2},
        headers=HDR,
    )
    body = resp.json()
    assert [it["agent_id"] for it in body["items"]] == ["ag-3"]

    # 翻页超出范围:空 items 不报错
    resp = client.get(
        "/agents/chat-concurrency",
        params={"pageNum": 9, "pageSize": 2},
        headers=HDR,
    )
    body = resp.json()
    assert body["total"] == 3
    assert body["items"] == []


def test_list_empty_when_nothing_configured(env):
    client, *_ = env
    resp = client.get("/agents/chat-concurrency", headers=HDR)
    assert resp.status_code == 200
    assert resp.json() == {
        "total": 0,
        "page_num": 1,
        "page_size": 10,
        "items": [],
    }


def test_list_agent_name_filter(env):
    """按 agent_name 模糊查询：只返回名称命中的配置，分页 total 也按过滤后计算。"""
    client, db, _, _ = env
    _seed_three(db)
    resp = client.get(
        "/agents/chat-concurrency",
        params={"agent_name": "数据", "pageNum": 1, "pageSize": 10},
        headers=HDR,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [it["agent_id"] for it in body["items"]] == ["ag-1"]
    assert body["items"][0]["agent_name"] == "数据分析助手"

    # 无匹配返回空 items，但 total 仍为 0
    resp = client.get(
        "/agents/chat-concurrency",
        params={"agent_name": "不存在的关键词"},
        headers=HDR,
    )
    assert resp.status_code == 200
    assert resp.json()["items"] == []

    # 不传 agent_name = 不过滤（与改造前行为一致）
    resp = client.get(
        "/agents/chat-concurrency",
        params={"pageNum": 1, "pageSize": 10},
        headers=HDR,
    )
    assert resp.json()["total"] == 3


# ── DELETE 恢复不限 ──────────────────────────────────────────────


def test_delete_resets_to_unlimited(env):
    client, db, cache, has_any = env
    db["ag-1"] = (5, time.time(), _AGENTS["ag-1"])
    cache["ag-1"] = 5
    resp = client.delete(
        "/agents/chat-concurrency",
        params={"agent_id": "ag-1"},
        headers=HDR,
    )
    assert resp.status_code == 200
    assert resp.json() == {"agent_id": "ag-1", "max_running": None}
    assert "ag-1" not in db
    assert "ag-1" not in cache
    # 最后一条被清 → has_any 翻回 False
    assert has_any["v"] is False


def test_delete_idempotent(env):
    client, *_ = env
    for _ in range(2):
        resp = client.delete(
            "/agents/chat-concurrency",
            params={"agent_id": "ag-3"},
            headers=HDR,
        )
        assert resp.status_code == 200


def test_delete_agent_not_found(env):
    client, *_ = env
    resp = client.delete(
        "/agents/chat-concurrency",
        params={"agent_id": "ghost"},
        headers=HDR,
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found"}
