# -*- coding: utf-8 -*-
"""runtime_config_store 通用配置读写层单测：SQLite 临时文件库替换 engine。

pytest-asyncio 未安装，异步用例统一用 ``asyncio.run()`` 包裹
（与 test_memory_config.py 一致）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp import runtime_config_store
from bocomadp.config.app_config import SummarizationConfig


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """替换 _get_engine 为临时文件 sqlite engine，并确保建表。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rc.db'}")

    async def fake_engine():
        return engine

    monkeypatch.setattr(runtime_config_store, "_get_engine", fake_engine)
    monkeypatch.setattr(runtime_config_store, "_initialized_for", None)
    return engine


def _run(coro):
    return asyncio.run(coro)


def test_set_and_get_roundtrip(sqlite_db):
    payload = {"enabled": True, "user_id": "lwh", "credential_id": "c1", "model_name": "m1"}
    _run(runtime_config_store.config_set("summarization", payload))
    got = _run(runtime_config_store.config_get("summarization"))
    assert got == payload


def test_get_missing_returns_none(sqlite_db):
    assert _run(runtime_config_store.config_get("no-such-key")) is None


def test_set_overwrites(sqlite_db):
    _run(runtime_config_store.config_set("summarization", {"enabled": True, "a": 1}))
    _run(runtime_config_store.config_set("summarization", {"enabled": False, "b": 2}))
    got = _run(runtime_config_store.config_get("summarization"))
    assert got == {"enabled": False, "b": 2}


def test_delete_returns_bool(sqlite_db):
    _run(runtime_config_store.config_set("k", {"x": 1}))
    assert _run(runtime_config_store.config_delete("k")) is True
    assert _run(runtime_config_store.config_get("k")) is None
    assert _run(runtime_config_store.config_delete("k")) is False


def test_list(sqlite_db):
    _run(runtime_config_store.config_set("a", {"x": 1}))
    _run(runtime_config_store.config_set("b", {"y": 2}))
    result = _run(runtime_config_store.config_list())
    assert result == {"a": {"x": 1}, "b": {"y": 2}}


def test_get_typed_config(sqlite_db):
    _run(runtime_config_store.config_set(
        "summarization",
        {
            "enabled": True,
            "user_id": "lwh",
            "credential_id": "c1",
            "model_name": "deepseek-v4-flash",
        },
    ))
    cfg = _run(runtime_config_store.get_typed_config("summarization", SummarizationConfig))
    assert isinstance(cfg, SummarizationConfig)
    assert cfg.model_name == "deepseek-v4-flash"


def test_get_typed_config_missing(sqlite_db):
    assert _run(runtime_config_store.get_typed_config("nope", SummarizationConfig)) is None
