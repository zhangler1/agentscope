# -*- coding: utf-8 -*-
"""memory/routers.py 单测（Task 13，§7.5）：越权修复 + 平台同步决策。

形态：FastAPI TestClient + lifespan 内装配真 SQL 后端（config_store 全链路）
+ 可编程假框架 storage（get_agent 归属表）+ monkeypatch 平台注册与全局参数
存储（全局参数真源为目标项目 ``runtime_configs``，本测试用内存字典打桩）。

- 越权：非 owner → 404（不暴露存在性）；自称 admin 无特权；
- 鉴权：全局端点未登录 → 422（deps 为必填 Header，缺省即校验失败）；
- 平台同步（优化 #4）：注册失败 → 200 + register_status=false +
  register_error + 配置已落库；注册成功 → caller 回写 +
  register_status=true；已有 caller 不重复注册；
- 新字段（优化 #7）：infer/update_rounds/top_k 落库回读一致（infer 为
  "0"/"1" 字符串，D10）。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bocomadp.memory import config_store as memory_store
from bocomadp.memory import platform as pf
from bocomadp.memory import routers


class _FakeFrameworkStorage:
    """假框架 storage：归属表 {(user_id, agent_id): agent} 可编程。"""

    def __init__(self) -> None:
        self.agents: dict[tuple[str, str], Any] = {}

    async def get_agent(self, user_id: str, agent_id: str):
        return self.agents.get((user_id, agent_id))


@pytest.fixture
def platform_register(monkeypatch) -> list:
    """monkeypatch 平台注册为可控 fake；返回调用记录列表。"""
    calls: list = []

    async def _register(request):
        calls.append(request)
        return "caller-1"

    monkeypatch.setattr(pf, "register_agent", _register)
    return calls


@pytest.fixture
def harness(platform_register, make_sql_storage):
    """TestClient + 可编程归属表。

    lifespan（TestClient 的事件循环）内装配 MySQL 后端——
    请求与装配同循环，避免 engine 跨 loop。
    """
    framework = _FakeFrameworkStorage()
    #: 默认归属：u1 拥有 agent1（用例以 u1 身份操作；越权用例自行清空）
    framework.agents[("u1", "agent1")] = object()
    saved = (memory_store._memory_storage, memory_store._storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        storage = make_sql_storage()
        await storage.ensure_ready()
        memory_store._memory_storage = storage
        memory_store._storage = framework
        yield
        await storage.close()

    app = FastAPI(lifespan=lifespan)
    routers.install(app)
    try:
        with TestClient(app) as client:
            yield client, framework
    finally:
        memory_store._memory_storage, memory_store._storage = saved


# ---------------------------------------------------------------------------
# 越权：非 owner → 404（含自称 admin）；owner 放行
# ---------------------------------------------------------------------------


def test_put_rejected_for_non_owner(harness, platform_register):
    """非 owner：agent 不属于该用户 → 404，且不落库不注册。"""
    client, framework = harness
    framework.agents = {}  # get_agent 无命中
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 404
    assert platform_register == []  # 未触达平台
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).status_code == 404  # 未落库


def test_admin_header_has_no_privilege(harness, platform_register):
    """自称 admin 不再放行：无归属即 404（防特权旁路回归）。"""
    client, framework = harness
    framework.agents = {}
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "admin"},
    )
    assert resp.status_code == 404
    assert platform_register == []  # 未触达平台
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "admin"}
    ).status_code == 404  # 未落库


def test_owner_can_access(harness, platform_register):
    """归属命中（框架 agent 表）：owner 正常读写。"""
    client, framework = harness
    framework.agents[("u1", "agent1")] = object()
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 200
    # GET 同样校验：owner 200，他人 404
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).status_code == 200
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u2"}
    ).status_code == 404


# ---------------------------------------------------------------------------
# 鉴权：全局端点未登录 → 422
# ---------------------------------------------------------------------------


def test_global_endpoints_require_login(harness):
    """全局端点未登录（无 X-User-ID）：deps 必填 Header 校验失败 → 422。"""
    client, _ = harness
    assert client.put("/memory/config", json={"idle_minutes": 20}).status_code == 422
    assert client.get("/memory/config").status_code == 422
    assert client.delete("/memory/config").status_code == 422


# ---------------------------------------------------------------------------
# 平台同步决策：注册失败 pending / 成功 synced / 不重复注册
# ---------------------------------------------------------------------------


def test_register_failure_returns_pending_and_persists(
    harness, monkeypatch
):
    """注册失败：不再 502——200 + register_status=false + register_error，配置已落库。"""
    client, _ = harness

    async def _fail(request):
        raise pf.MemoriesSdkError("memory: boom")

    async def _ok(request):
        return "caller-1"

    monkeypatch.setattr(pf, "register_agent", _fail)
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["register_status"] is False
    assert "boom" in body["register_error"]
    assert body["extraction_enabled"] is True
    assert body["caller"] == ""
    # 本地已持久化（下次 PUT 自动补注册）
    got = client.get("/memory/config/agent1", headers={"X-User-ID": "u1"})
    assert got.status_code == 200
    assert got.json()["extraction_enabled"] is True
    # 下次 PUT（平台恢复）自动补注册成功
    monkeypatch.setattr(pf, "register_agent", _ok)
    resp2 = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp2.json()["register_status"] is True
    assert resp2.json()["caller"] == "caller-1"


def test_register_success_writes_back_caller(harness, platform_register):
    """注册成功：caller 回写落库 + "synced"；agentName 透传注册请求。"""
    client, _ = harness
    resp = client.put(
        "/memory/config/agent1",
        json={
            "extraction_enabled": True,
            "agentName": "演示助手",
            "memory_prompt": "请记住用户的偏好",
        },
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["register_status"] is True
    assert body["caller"] == "caller-1"
    assert body["memory_prompt"] == "请记住用户的偏好"
    # 请求对象断言（mapping 单一真源）
    request = platform_register[0]
    assert request.agent_id == "agent1"
    assert request.agent_name == "演示助手"
    assert request.memory_prompt == "请记住用户的偏好"
    # caller 已回写落库
    got = client.get("/memory/config/agent1", headers={"X-User-ID": "u1"})
    assert got.json()["caller"] == "caller-1"


def test_existing_caller_no_repeat_register(harness, platform_register):
    """已有 caller（曾注册成功）：再次 PUT 不重复注册。"""
    client, _ = harness
    client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert len(platform_register) == 1
    resp = client.put(
        "/memory/config/agent1",
        json={"update_rounds": 5},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 200
    assert resp.json()["register_status"] is True
    assert resp.json()["update_rounds"] == 5
    assert len(platform_register) == 1  # 未重复注册


def test_disabled_never_registers(harness, platform_register):
    """双开关全关：不注册（避免白注册）。"""
    client, _ = harness
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": False},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 200
    assert resp.json()["extraction_enabled"] is False
    assert resp.json()["register_status"] is False  # 双关不注册
    assert resp.json()["register_error"] is None
    assert platform_register == []


def test_enable_after_disabled_registers_once(harness, platform_register):
    """先 disabled 落库（不注册），再 enabled → 恰好注册一次；epoch +2。"""
    client, _ = harness
    client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": False},
        headers={"X-User-ID": "u1"},
    )
    assert platform_register == []
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp.json()["caller"] == "caller-1"
    assert len(platform_register) == 1
    # 首次落库即 False（无变化不 +1）；False→True 变化一次 → epoch=1（§6.1）
    assert resp.json()["switch_epoch"] == 1


# ---------------------------------------------------------------------------
# 新字段（优化 #7）：payload 全字段落库回读一致
# ---------------------------------------------------------------------------


def test_put_new_fields_roundtrip(harness):
    """infer/update_rounds/top_k 落库回读一致（infer 为 "0"/"1" 字符串，D10）。"""
    client, _ = harness
    payload = {
        "extraction_enabled": True,
        "infer": "0",
        "update_rounds": 5,
        "top_k": 9,
    }
    resp = client.put(
        "/memory/config/agent1", json=payload, headers={"X-User-ID": "u1"}
    )
    assert resp.status_code == 200
    got = client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).json()
    assert got["infer"] == "0"
    assert got["update_rounds"] == 5
    assert got["top_k"] == 9


def test_removed_register_fields_rejected(harness):
    """已删除的注册期可选字段（data_type/file_system/parse_config）→ 422。

    Record 为 extra=forbid，旧字段不再被静默接受（与字段改名的既有风格
    一致）——前端需同步下掉这三个字段。
    """
    client, _ = harness
    for field, value in (
        ("data_type", 2),
        ("file_system", 1),
        ("parse_config", '{"splitter": "line"}'),
    ):
        resp = client.put(
            "/memory/config/agent1",
            json={field: value},
            headers={"X-User-ID": "u1"},
        )
        assert resp.status_code == 422, field


# ---------------------------------------------------------------------------
# 基础回归：部分更新 / 未知字段 / 删除 / 全局参数
# ---------------------------------------------------------------------------


def test_put_partial_update_keeps_existing(harness, platform_register):
    """partial payload：未传字段保持既有值。"""
    client, _ = harness
    client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True, "top_k": 8},
        headers={"X-User-ID": "u1"},
    )
    resp = client.put(
        "/memory/config/agent1",
        json={"update_rounds": 3},
        headers={"X-User-ID": "u1"},
    )
    body = resp.json()
    assert body["top_k"] == 8  # 保持
    assert body["update_rounds"] == 3  # 更新


def test_put_unknown_field_rejected(harness, platform_register):
    """未知字段：Record extra=forbid → 422，且不落库。"""
    client, _ = harness
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True, "unknown_field": 1},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 422
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).status_code == 404


def test_legacy_field_memory_enabled_rejected(harness, platform_register):
    """旧字段 memory_enabled（Record extra=forbid）→ 422（无兼容，§13.3）。"""
    client, _ = harness
    resp = client.put(
        "/memory/config/agent1",
        json={"memory_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp.status_code == 422
    assert platform_register == []


def test_switch_epoch_increments_only_on_extraction_toggle(harness):
    """switch_epoch 仅 extraction_enabled 变化时 +1；客户端传入被忽略（§6.1）。"""
    client, _ = harness
    resp = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True, "switch_epoch": 99},
        headers={"X-User-ID": "u1"},
    )
    body = resp.json()
    assert body["switch_epoch"] == 1  # 客户端传入 99 被忽略
    # 注入开关切换不推进 epoch
    resp2 = client.put(
        "/memory/config/agent1",
        json={"injection_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    assert resp2.json()["switch_epoch"] == 1
    # 关闭再 +1
    resp3 = client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": False},
        headers={"X-User-ID": "u1"},
    )
    assert resp3.json()["switch_epoch"] == 2


def test_delete_then_get_404(harness, platform_register):
    """DELETE：越权校验 + 删除后 GET 404。"""
    client, framework = harness
    framework.agents[("u1", "agent1")] = object()
    client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )
    # 他人删除 → 404（不暴露存在性）
    assert client.delete(
        "/memory/config/agent1", headers={"X-User-ID": "u2"}
    ).status_code == 404
    # owner 删除 → 204，此后 GET 404
    assert client.delete(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).status_code == 204
    assert client.get(
        "/memory/config/agent1", headers={"X-User-ID": "u1"}
    ).status_code == 404


def test_delete_also_purges_session_states(harness):
    """DELETE：配置与全部会话状态一起清（不再是只删配置）。"""
    client, framework = harness
    framework.agents[("u1", "agent1")] = object()
    client.put(
        "/memory/config/agent1",
        json={"extraction_enabled": True},
        headers={"X-User-ID": "u1"},
    )

    async def _seed() -> None:
        storage = memory_store._memory_storage
        assert storage is not None
        await storage.ensure_memory_session_state(
            "u1",
            "agent1",
            "sess1",
            cursor_created_at="",
            cursor_msg_id="",
            epoch=1,
            now=time.time(),
        )

    client.portal.call(_seed)

    assert client.delete(
        "/memory/config/agent1",
        headers={"X-User-ID": "u1"},
    ).status_code == 204

    async def _state():
        storage = memory_store._memory_storage
        assert storage is not None
        return await storage.get_memory_session_state(
            "u1",
            "agent1",
            "sess1",
        )

    assert client.portal.call(_state) is None
    assert client.get(
        "/memory/config/agent1",
        headers={"X-User-ID": "u1"},
    ).status_code == 404


def test_global_config_roundtrip(harness, monkeypatch):
    """全局参数：PUT 合并落库 → GET 读回；DELETE 回退默认。

    全局参数真源为目标项目 ``runtime_configs``（经 bocomadp.runtime_config_store），
    本用例用内存字典打桩，避免测试依赖真实库表。
    """
    from bocomadp.memory import global_config_store as gcs
    from bocomadp.memory.config import MemoryGlobalConfig

    state: dict = {}

    async def _get() -> MemoryGlobalConfig:
        return MemoryGlobalConfig(**state) if state else MemoryGlobalConfig()

    async def _upsert(cfg: MemoryGlobalConfig) -> MemoryGlobalConfig:
        state.clear()
        state.update(cfg.model_dump())
        return cfg

    async def _delete() -> bool:
        had = bool(state)
        state.clear()
        return had

    monkeypatch.setattr(gcs, "get_memory_global_config", _get)
    monkeypatch.setattr(gcs, "upsert_memory_global_config", _upsert)
    monkeypatch.setattr(gcs, "delete_memory_global_config", _delete)

    client, _ = harness
    headers = {"X-User-ID": "u1"}
    resp = client.put("/memory/config", json={"idle_minutes": 20}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["idle_minutes"] == 20
    got = client.get("/memory/config", headers=headers).json()
    assert got["idle_minutes"] == 20
    # partial：未传字段保持
    client.put("/memory/config", json={"max_tokens": 30000}, headers=headers)
    got = client.get("/memory/config", headers=headers).json()
    assert got["idle_minutes"] == 20
    assert got["max_tokens"] == 30000
    # 删除 → 回退默认
    assert client.delete("/memory/config", headers=headers).status_code == 204
    got = client.get("/memory/config", headers=headers).json()
    assert got["idle_minutes"] == 60  # 代码默认值
