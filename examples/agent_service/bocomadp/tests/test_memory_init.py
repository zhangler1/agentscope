# -*- coding: utf-8 -*-
"""memory/__init__.py 装配 API 单测（Task 14，§7.6）。

覆盖：setup_memory 装配表（Redis/SQL/显式覆盖/fail-fast）、
build_memory_middlewares 开关语义、
cleanup_agent_memory 反查清理、install_memory 路由挂载 + lifespan 启停。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fakeredis import aioredis as fake_aioredis
from sqlalchemy.ext.asyncio import create_async_engine

import bocomadp.memory as memory
from bocomadp.memory import config_store as memory_store
from bocomadp.memory import platform as pf
from bocomadp.memory.middleware import MemoryMiddleware
from bocomadp.memory.settings import MemorySettings, reset_settings
from bocomadp.memory.storage._models import MemoryAgentConfigRecord
from bocomadp.memory.storage._redis_memory import RedisMemoryStorage
from bocomadp.memory.storage._sql_memory import SQLMemoryStorage


class _FakeBus:
    """最小 MessageBus 形态（is_locked / acquire_lock）。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def is_locked(self, key: str) -> bool:
        del key
        return self._lock.locked()


@pytest.fixture(autouse=True)
def _reset_runtime():
    """每个用例后还原全局装配单例，避免跨用例泄漏。"""
    yield
    memory_store._memory_storage = None
    memory_store._storage = None
    memory_store._message_bus = None
    reset_settings()


# ---------------------------------------------------------------------------
# setup_memory 装配表
# ---------------------------------------------------------------------------


def test_configure_redis_backend():
    """RedisStorage → RedisMemoryStorage(get_client())，三引用齐备。"""
    from agentscope.app.storage._redis_storage import RedisStorage

    rs = RedisStorage()
    rs._client = fake_aioredis.FakeRedis()  # 模拟已 __aenter__
    bus = _FakeBus()
    memory.setup_memory(MemorySettings(environment="sit"), rs, bus)
    assert isinstance(memory_store._memory_storage, RedisMemoryStorage)
    assert memory_store._storage is rs
    assert memory_store._message_bus is bus


def test_configure_redis_client_not_ready_raises():
    """RedisStorage 未 __aenter__（client None）→ fail-fast。"""
    from agentscope.app.storage._redis_storage import RedisStorage

    with pytest.raises(RuntimeError, match="get_client"):
        memory.setup_memory(MemorySettings(environment="sit"), RedisStorage(), _FakeBus())


def test_configure_sql_backend_db_url(sql_db_url):
    """AsyncSQLAlchemyStorage + settings.db_url → SQL 自建 engine 后端。"""
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage

    framework = AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:")
    memory.setup_memory(
        MemorySettings(environment="sit", db_url=sql_db_url),
        framework,
        _FakeBus(),
    )
    ms = memory_store._memory_storage
    assert isinstance(ms, SQLMemoryStorage)
    assert ms._own_engine is True  # 自建（close 时 dispose）


def test_configure_sql_backend_engine_shared(sql_db_url):
    """settings.engine 共享 → SQLMemoryStorage 不拥有 engine 生命周期。"""
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage

    framework = AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(sql_db_url)
    try:
        memory.setup_memory(
            MemorySettings(environment="sit", engine=engine), framework, _FakeBus()
        )
        ms = memory_store._memory_storage
        assert isinstance(ms, SQLMemoryStorage)
        assert ms._own_engine is False  # 共享（close 不 dispose）
    finally:
        asyncio.run(engine.dispose())


def test_configure_sql_without_db_url_raises():
    """SQL 框架后端但 db_url/engine 皆空 → ValueError（fail-fast）。"""
    from agentscope.app.storage._sql import AsyncSQLAlchemyStorage

    framework = AsyncSQLAlchemyStorage("sqlite+aiosqlite:///:memory:")
    with pytest.raises(ValueError, match="db_url"):
        memory.setup_memory(MemorySettings(environment="sit"), framework, _FakeBus())


def test_configure_unsupported_storage_raises():
    """未知框架 storage 类型且未显式传 → TypeError（fail-fast）。"""
    with pytest.raises(TypeError, match="不支持"):
        memory.setup_memory(MemorySettings(environment="sit"), object(), _FakeBus())


def test_settings_environment_required():
    """部署配置必填项：environment 缺省或空串 → pydantic 校验失败。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MemorySettings()                    # 缺 environment
    with pytest.raises(ValidationError):
        MemorySettings(environment="")      # 空串等同未配置


def test_get_settings_before_setup_raises():
    """未装配时读取部署配置 → RuntimeError（不再静默回退默认值）。"""
    from bocomadp.memory.settings import get_settings

    reset_settings()
    with pytest.raises(RuntimeError, match="未注入"):
        get_settings()


def test_configure_explicit_memory_storage_wins():
    """显式 memory_storage 直通（测试/自定义后端），不做类型推断。"""
    explicit = RedisMemoryStorage(
        fake_aioredis.FakeRedis(), state_ttl_secs=86400
    )
    memory.setup_memory(
        MemorySettings(environment="sit"), object(), _FakeBus(), memory_storage=explicit
    )
    assert memory_store._memory_storage is explicit


# ---------------------------------------------------------------------------
# build_memory_middlewares
# ---------------------------------------------------------------------------


def test_build_memory_middlewares_not_configured():
    """未装配（configure 未调用）→ []（fail-open，不阻塞对话）。"""
    assert asyncio.run(memory.build_memory_middlewares("u1", "a1", "s1")) == []


def test_build_memory_middlewares_switch(make_sql_storage, sql_db_url):
    """装配后按配置开关：无配置/未启用 → []；启用 → [MemoryMiddleware]。"""

    async def scenario() -> list:
        storage = make_sql_storage()
        await storage.ensure_ready()
        memory.setup_memory(
            MemorySettings(environment="sit", db_url=sql_db_url),
            object(),
            _FakeBus(),
            memory_storage=storage,
        )
        # 无配置 → []
        empty = await memory.build_memory_middlewares("u1", "a1", "s1")
        # 双开关全关 → []
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(agent_id="a1", injection_enabled=False, extraction_enabled=False)
        )
        disabled = await memory.build_memory_middlewares("u1", "a1", "s1")
        # 提取开启 → [MemoryMiddleware]
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True)
        )
        enabled = await memory.build_memory_middlewares("u1", "a1", "s1")
        return [empty, disabled, enabled]

    empty, disabled, enabled = asyncio.run(scenario())
    assert empty == []
    assert disabled == []
    assert len(enabled) == 1
    mw = enabled[0]
    assert isinstance(mw, MemoryMiddleware)
    assert (mw.user_id, mw.agent_id, mw.session_id) == ("u1", "a1", "s1")


# ---------------------------------------------------------------------------
# cleanup_agent_memory
# ---------------------------------------------------------------------------


def test_cleanup_agent_memory_scoped(make_sql_storage, sql_db_url):
    """删配置 + 反查清该 agent 会话状态；其他 agent 会话不受影响。"""

    async def scenario() -> dict:
        storage = make_sql_storage()
        await storage.ensure_ready()
        memory.setup_memory(
            MemorySettings(environment="sit", db_url=sql_db_url),
            object(),
            _FakeBus(),
            memory_storage=storage,
        )
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True)
        )
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(agent_id="a2", extraction_enabled=True)
        )
        # a1 两个会话 + a2 一个会话（不应被清理）
        for session in ("s1", "s2"):
            await storage.ensure_memory_session_state(
                "u1", "a1", session,
                cursor_created_at="", cursor_msg_id="", epoch=0, now=1000.0,
            )
        await storage.ensure_memory_session_state(
            "u1", "a2", "s9",
            cursor_created_at="", cursor_msg_id="", epoch=0, now=1000.0,
        )
        await memory.cleanup_agent_memory("a1")
        # 直读表枚举（列举端口已收敛为"提取候选"专用查询，不作校验用）
        from sqlalchemy import select

        from bocomadp.memory.storage._sql_memory import _SessionStateRow

        async with storage._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        _SessionStateRow.agent_id,
                        _SessionStateRow.session_id,
                    )
                )
            ).all()
        cfg_a1 = await storage.get_memory_config("a1")
        cfg_a2 = await storage.get_memory_config("a2")
        return {
            "a1_states": [r.session_id for r in rows if r.agent_id == "a1"],
            "a2_states": [r.session_id for r in rows if r.agent_id == "a2"],
            "cfg_a1": cfg_a1,
            "cfg_a2": cfg_a2,
        }

    result = asyncio.run(scenario())
    assert result["a1_states"] == []  # a1 会话状态已清
    assert len(result["a2_states"]) == 1  # a2 不受影响
    assert result["cfg_a1"] is None  # a1 配置已删
    assert result["cfg_a2"] is not None


def test_cleanup_agent_memory_tolerates_cleanup_failure(
    make_sql_storage, sql_db_url, monkeypatch
):
    """清理会话状态失败不冒泡：配置已删，残留交给 TTL / 超龄清理兜底。

    框架的 agent 删除链路不应因记忆侧清理失败而失败（§2.7）。
    """

    async def scenario() -> object:
        storage = make_sql_storage()
        await storage.ensure_ready()
        memory.setup_memory(
            MemorySettings(environment="sit", db_url=sql_db_url),
            object(),
            _FakeBus(),
            memory_storage=storage,
        )
        await storage.upsert_memory_config(
            MemoryAgentConfigRecord(agent_id="a1", extraction_enabled=True)
        )

        async def _boom(agent_id: str) -> int:
            raise RuntimeError("storage down")

        monkeypatch.setattr(storage, "delete_agent_session_states", _boom)
        await memory.cleanup_agent_memory("a1")  # 不抛
        return await storage.get_memory_config("a1")

    assert asyncio.run(scenario()) is None  # 配置仍已删除


def test_cleanup_agent_memory_not_configured():
    """未装配：delete_memory_config 宽松返回，不抛异常。"""
    asyncio.run(memory.cleanup_agent_memory("a1"))


# ---------------------------------------------------------------------------
# install_memory：路由挂载 + lifespan 启停
# ---------------------------------------------------------------------------


def test_install_memory_mounts_routes_and_lifespan(monkeypatch):
    """挂 /memory/config 路由；lifespan 启停 SDK client（未装配时 sweeper 跳过）。"""
    calls: list[str] = []

    async def _fake_start(settings=None):
        calls.append("start")

    async def _fake_close():
        calls.append("close")

    monkeypatch.setattr(pf, "start_client", _fake_start)
    monkeypatch.setattr(pf, "close_client", _fake_close)

    app = FastAPI()
    memory.install_memory(app)
    # 路由已挂载（GET 全局参数——未装配读侧宽松返回默认）
    with TestClient(app) as client:
        resp = client.get("/memory/config", headers={"X-User-ID": "u1"})
        assert resp.status_code == 200
        assert resp.json()["idle_minutes"] == 60  # 默认值
    assert calls == ["start", "close"]  # lifespan 启停成对
