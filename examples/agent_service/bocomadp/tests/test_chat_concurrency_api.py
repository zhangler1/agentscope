# -*- coding: utf-8 -*-
"""智能体 /chat 并发上限管理接口测试（PUT/GET列表分页/GET单条/DELETE）。

轻量装配：FastAPI 直挂 ``agent_chat_concurrency_router``，PG 读写、
运行时热层与角色判据全部 monkeypatch（不碰真库）；依赖
``get_current_user_id`` 只读 ``X-User-ID`` header 的特性。

pytest-asyncio 未安装：异步逻辑用 asyncio.run() 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bocomadp import chat_concurrency_config, role_store
from bocomadp.routers.agent_chat_concurrency import (
    agent_chat_concurrency_router,
)

ADMIN = "reviewer-1"
HDR = {"X-User-ID": ADMIN}
HDR_USER = {"X-User-ID": "test-user"}

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
    """内存桩：PG 表 db、运行时缓存 cache。"""
    db: dict[str, tuple[int, float]] = {}
    cache: dict[str, int] = {}

    async def pg_upsert(agent_id: str, max_running: int, agent_name="") -> None:
        db[agent_id] = (max_running, time.time(), agent_name)

    async def pg_get_full(agent_id: str):
        return db.get(agent_id)

    async def pg_delete(agent_id: str) -> None:
        db.pop(agent_id, None)

    async def pg_list_all() -> dict[str, tuple[int, float, str]]:
        return dict(db)

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

    # 默认所有智能体均视为已审核通过；未过审场景由单测临时覆盖
    async def is_agent_approved(agent_id: str) -> bool:
        return True

    async def has_role(user_id: str, role_key: str) -> bool:
        return user_id == ADMIN and role_key == role_store.REVIEWER_ROLE

    monkeypatch.setattr(role_store, "has_role", has_role)

    for name, fn in [
        ("pg_upsert", pg_upsert),
        ("pg_get_full", pg_get_full),
        ("pg_delete", pg_delete),
        ("pg_list_all", pg_list_all),
        ("pg_list_filtered", pg_list_filtered),
        ("pg_agent_names", pg_agent_names),
        ("set_runtime", set_runtime),
        ("delete_runtime", delete_runtime),
        ("get_effective", get_effective),
        ("is_agent_approved", is_agent_approved),
    ]:
        monkeypatch.setattr(chat_concurrency_config, name, fn)

    app = FastAPI()
    app.include_router(agent_chat_concurrency_router)
    app.state.storage = _FakeStorage()
    with TestClient(app) as test_client:
        yield test_client, db, cache


def _run(coro):
    return asyncio.run(coro)


# ── PUT 设置上限 ─────────────────────────────────────────────────


def test_put_sets_limit(env):
    client, db, cache = env
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


def test_put_allows_zero_as_explicit_unlimited(env):
    client, db, cache = env
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


def test_put_rejects_unapproved_agent(env, monkeypatch):
    """未过审智能体调配置接口 → 403 拒绝、配置不落库。

    前端配置页已屏蔽未过审智能体，此拦截兜底层直连接口绕过前端的场景，
    避免未过审时存的配置在审核通过后悄悄生效。
    """
    client, db, cache = env

    async def _unapproved(agent_id: str) -> bool:
        return agent_id != "ag-2"

    monkeypatch.setattr(chat_concurrency_config, "is_agent_approved", _unapproved)

    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-2", "max_running": 5},
        headers=HDR,
    )
    assert resp.status_code == 403
    assert "仅审核通过" in resp.json()["detail"]
    # 既不写 PG 也不写运行时缓存
    assert "ag-2" not in db
    assert "ag-2" not in cache


def test_put_rejects_when_approval_state_unknown(env, monkeypatch):
    """审批状态暂不可用（查库失败）→ PUT 403 拒绝、配置不落库。

    is_agent_approved 在状态未知时按 fail-closed 返回 False，接口层据此
    挡住故障窗口内绕过前端的配置写入。
    """
    client, db, cache = env

    async def _unknown(agent_id: str) -> bool:
        return False

    monkeypatch.setattr(chat_concurrency_config, "is_agent_approved", _unknown)

    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-1", "max_running": 5},
        headers=HDR,
    )
    assert resp.status_code == 403
    assert "审批状态暂不可用" in resp.json()["detail"]
    assert "ag-1" not in db
    assert "ag-1" not in cache


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
    client, db, _ = env
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
    client, db, _ = env
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
    client, db, cache = env
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
    # 最后一条被清 → PG 与运行时缓存均已删除



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


# ── 管理员门禁（三个接口统一 403） ────────────────────────────────


def test_non_admin_blocked_on_all_three_endpoints(env):
    """非管理员 PUT / GET / DELETE 一律 403，配置不落库、列表不泄露。"""
    client, db, cache = env
    db["ag-1"] = (5, time.time(), _AGENTS["ag-1"])

    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-2", "max_running": 5},
        headers=HDR_USER,
    )
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]
    assert "ag-2" not in db and "ag-2" not in cache

    resp = client.get("/agents/chat-concurrency", headers=HDR_USER)
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]

    resp = client.delete(
        "/agents/chat-concurrency",
        params={"agent_id": "ag-1"},
        headers=HDR_USER,
    )
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]
    assert "ag-1" in db


def test_gate_runs_before_agent_lookup(env):
    """门禁先于智能体存在性校验：非管理员传不存在的 id 也是 403，不是 404。"""
    client, *_ = env
    resp = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ghost", "max_running": 5},
        headers=HDR_USER,
    )
    assert resp.status_code == 403
    resp = client.delete(
        "/agents/chat-concurrency",
        params={"agent_id": "ghost"},
        headers=HDR_USER,
    )
    assert resp.status_code == 403


def test_admin_full_flow_with_role_gate(env):
    """管理员 PUT → GET → DELETE → GET 全链路 200；非管理员读仍 403。"""
    client, db, cache = env
    put = client.put(
        "/agents/chat-concurrency",
        json={"agent_id": "ag-1", "max_running": 5},
        headers=HDR,
    )
    assert put.status_code == 200
    assert cache["ag-1"] == 5

    listed = client.get("/agents/chat-concurrency", headers=HDR)
    assert listed.status_code == 200
    assert [it["agent_id"] for it in listed.json()["items"]] == ["ag-1"]
    assert client.get(
        "/agents/chat-concurrency", headers=HDR_USER,
    ).status_code == 403

    deleted = client.delete(
        "/agents/chat-concurrency",
        params={"agent_id": "ag-1"},
        headers=HDR,
    )
    assert deleted.status_code == 200
    assert "ag-1" not in db and "ag-1" not in cache
    assert client.get(
        "/agents/chat-concurrency", headers=HDR,
    ).json()["items"] == []
