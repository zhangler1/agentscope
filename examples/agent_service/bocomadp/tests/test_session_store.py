# -*- coding: utf-8 -*-
"""_session_store 共享存储测试（fakeredis，不发真实网络）。"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from bocomadp.deerflow import _session_store
from bocomadp.deerflow._session_store import load_auth, load_params, save_session
from bocomadp.deerflow.auth_context import ResolvedAuth


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """每个测试用独立 fakeredis 实例替换模块级 _redis。"""
    fr = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_session_store, "_redis", fr)
    yield fr
    fr.flushall()


def _run(coro):
    return asyncio.run(coro)


def test_save_and_load_params_roundtrip():
    params = {"vector_search_switch": True, "space_code_list": ["SP1"]}
    _run(save_session("sid-1", params=params))
    assert _run(load_params("sid-1")) == params


def test_load_missing_session_returns_none():
    assert _run(load_params("nope")) is None
    assert _run(load_auth("nope")) is None


def test_upsert_preserves_other_field():
    auth = ResolvedAuth(auth_mode="guwp-token", guwp_token="tok")
    _run(save_session("sid-2", params={"a": 1}))
    _run(save_session("sid-2", auth=auth))          # 只更新 auth
    assert _run(load_params("sid-2")) == {"a": 1}
    assert _run(load_auth("sid-2")) == auth
    _run(save_session("sid-2", params={"b": 2}))    # 只更新 params
    assert _run(load_params("sid-2")) == {"b": 2}
    assert _run(load_auth("sid-2")) == auth


def test_expire_sets_ttl(_fake_redis):
    _run(save_session("sid-3", params={"x": 1}))
    key = _session_store._key("sid-3")
    ttl = _run(_fake_redis.ttl(key))
    assert 0 < ttl <= _session_store._TTL_SECONDS
    # 模拟过期：直接重写 key 的 ttl 为 0（fakeredis 支持 expire 后立即查）
    _run(_fake_redis.expire(key, -1))
    assert _run(load_params("sid-3")) is None


def test_redis_unavailable_fail_open(monkeypatch):
    async def _boom(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(_session_store, "_get_redis", _boom)
    assert _run(load_params("sid")) is None        # 不抛异常
    assert _run(load_auth("sid")) is None
    _run(save_session("sid", params={"a": 1}))     # save 也不抛
