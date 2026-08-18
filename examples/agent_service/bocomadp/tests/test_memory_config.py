# -*- coding: utf-8 -*-
"""memory_config 存储层单测：SQLite 临时文件库替换 engine。

pytest-asyncio 未安装，异步用例统一用 ``asyncio.run()`` 包裹
（与 test_agent_ellm_integration.py 一致）。
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp import memory_config
from bocomadp.memory_config import MemoryConfig


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """替换 _get_engine 为临时文件 sqlite engine。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mem.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(memory_config, "_get_engine", fake_engine)
    return engine


def _run(coro):
    return asyncio.run(coro)


def test_upsert_and_get_roundtrip(sqlite_db):
    config = MemoryConfig(
        memory_update_prompt="更新记忆时参考用户画像",
        memory_enabled=True,
        memory_type=1,
        memory_update_rounds=5,
    )
    _run(memory_config.memory_upsert("agent-a", config))
    got = _run(memory_config.memory_get("agent-a"))
    assert got is not None
    assert got.model_dump() == config.model_dump()


def test_get_missing_returns_none(sqlite_db):
    assert _run(memory_config.memory_get("no-such-agent")) is None


def test_upsert_overwrites(sqlite_db):
    _run(memory_config.memory_upsert("agent-a", MemoryConfig(memory_enabled=True)))
    _run(memory_config.memory_upsert("agent-a", MemoryConfig(memory_enabled=False)))
    got = _run(memory_config.memory_get("agent-a"))
    assert got is not None and got.memory_enabled is False


def test_delete_removes_record(sqlite_db):
    _run(memory_config.memory_upsert("agent-a", MemoryConfig()))
    assert _run(memory_config.memory_delete("agent-a")) is True
    assert _run(memory_config.memory_get("agent-a")) is None
    # 幂等：二次删除返回 False
    assert _run(memory_config.memory_delete("agent-a")) is False


def test_memory_type_validation():
    with pytest.raises(ValidationError):
        MemoryConfig(memory_type=2)


def test_memory_rounds_validation():
    with pytest.raises(ValidationError):
        MemoryConfig(memory_update_rounds=-1)


def test_defaults():
    c = MemoryConfig()
    assert c.memory_update_prompt == ""
    assert c.memory_enabled is False
    assert c.memory_type == 0
    assert c.memory_update_rounds == 10
