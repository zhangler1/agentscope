# -*- coding: utf-8 -*-
"""_session_store: run_context 字段 + TTL 从 PG runtime_configs 读取。"""
import asyncio
import json

import pytest

import bocomadp.deerflow._session_store as store_mod


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis.aioredis as fk

    r = fk.FakeRedis(decode_responses=True)
    monkeypatch.setattr(store_mod, "_redis", r)
    return r


@pytest.fixture
def no_pg_ttl(monkeypatch):
    async def _none(key, model_cls):
        return None

    monkeypatch.setattr("bocomadp.runtime_config_store.get_typed_config", _none)


def _run(coro):
    return asyncio.run(coro)


def test_save_run_context_sets_field(fake_redis, no_pg_ttl):
    _run(store_mod.save_session("s1", run_context={"subagent_enabled": False}))
    raw = _run(fake_redis.hget("bocomadp:session:s1:custom_params", "run_context"))
    assert json.loads(raw) == {"subagent_enabled": False}


def test_load_run_context_roundtrip(fake_redis, no_pg_ttl):
    _run(store_mod.save_session("s1", run_context={"is_plan_mode": False}))
    assert _run(store_mod.load_run_context("s1")) == {"is_plan_mode": False}


def test_save_keeps_params_and_run_context_fields(fake_redis, no_pg_ttl):
    _run(store_mod.save_session("s1", params={"foo": 1}, run_context={"mode": "x"}))
    _run(store_mod.save_session("s1", run_context={"mode": "y"}))
    raw = _run(fake_redis.hget("bocomadp:session:s1:custom_params", "params"))
    assert json.loads(raw) == {"foo": 1}  # params 字段不被 run_context 覆盖


def test_ttl_from_pg(fake_redis, monkeypatch):
    from bocomadp.deerflow._session_store import SessionStoreConfig

    async def _cfg(key, model_cls):
        return SessionStoreConfig(ttl_seconds=7200)

    monkeypatch.setattr("bocomadp.runtime_config_store.get_typed_config", _cfg)
    _run(store_mod.save_session("s1", params={"a": 1}))
    assert _run(fake_redis.ttl("bocomadp:session:s1:custom_params")) == 7200


def test_ttl_default_when_no_pg_record(fake_redis, no_pg_ttl):
    _run(store_mod.save_session("s1", params={"a": 1}))
    ttl = _run(fake_redis.ttl("bocomadp:session:s1:custom_params"))
    assert ttl == 14400


def test_load_run_context_missing_returns_none(fake_redis, no_pg_ttl):
    assert _run(store_mod.load_run_context("ghost")) is None
