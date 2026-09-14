# -*- coding: utf-8 -*-
"""memory/store.py 存储层单测：SQLite 临时库替换 engine（同旧 test_memory_config.py 模式）。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.memory import store as memory_store
from bocomadp.memory.store import MemoryConfig


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mem.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(memory_store, "_get_engine", fake_engine)
    return engine


def _run(coro):
    return asyncio.run(coro)


def test_defaults_and_diff_dump():
    cfg = MemoryConfig()
    assert cfg.memory_enabled is False and cfg.update_rounds == 10
    # 模型层 exclude_defaults 语义保持（供可能的前端/接口差异使用）
    assert cfg.model_dump(exclude_defaults=True) == {}


def test_upsert_persists_full_payload(sqlite_db):
    """全量落库：即使全默认值字段也写入 payload（便于直接查库核对）。"""
    import json as _json

    from sqlalchemy import text as _text

    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig()))

    async def _read():
        engine = await memory_store._get_engine()
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    _text(
                        "SELECT payload FROM agent_memory_configs "
                        "WHERE agent_id = 'a1'",
                    ),
                )
            ).mappings().first()
        return row["payload"]

    raw = _run(_read())
    assert _json.loads(raw) == MemoryConfig().model_dump()


def test_memory_config_is_agent_scoped_across_users(sqlite_db):
    """记忆配置按 agent_id 定位：不同 user 也能读/删同一 agent 的配置。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    got = _run(memory_store.memory_get("a1"))  # 非写入者 user 也能读到
    assert got is not None and got.memory_enabled is True
    assert _run(memory_store.memory_delete("a1")) is True
    assert _run(memory_store.memory_get("a1")) is None


def test_upsert_get_delete_roundtrip(sqlite_db):
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    got = _run(memory_store.memory_get("a1"))
    assert got is not None and got.memory_enabled is True and got.top_k == 5
    assert _run(memory_store.memory_delete("a1")) is True
    assert _run(memory_store.memory_get("a1")) is None


def test_extra_allow_keeps_new_fields(sqlite_db):
    cfg = MemoryConfig(memory_enabled=True, **{"brand_new_field": "x"})
    _run(memory_store.memory_upsert("u1", "a1", cfg))
    got = _run(memory_store.memory_get("a1"))
    assert got.brand_new_field == "x"  # extra=allow 动态字段保留


def test_runtime_config_defaults():
    from bocomadp.memory.config import MemoryRuntimeConfig

    assert MemoryRuntimeConfig().max_tokens == 90000


def test_memory_list_enabled_returns_agent_cfg_pairs(sqlite_db):
    """memory_list_enabled 返回 (agent_id, cfg)；关闭项不返回。"""
    _run(memory_store.memory_upsert("u1", "a1", MemoryConfig(memory_enabled=True)))
    _run(memory_store.memory_upsert("u2", "a2", MemoryConfig(memory_enabled=False)))
    rows = _run(memory_store.memory_list_enabled())
    assert len(rows) == 1
    assert rows[0][0] == "a1"
    assert rows[0][1].memory_enabled is True
