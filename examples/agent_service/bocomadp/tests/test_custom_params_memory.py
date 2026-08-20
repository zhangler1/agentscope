# -*- coding: utf-8 -*-
"""custom_params Redis 化测试：save/load 委托 _session_store，ContextVar 不变。"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from bocomadp.deerflow import _session_store
from bocomadp.deerflow.custom_params import (
    get_custom_params,
    load_custom_params,
    reset_custom_params,
    save_custom_params,
    set_custom_params,
)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fr = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_session_store, "_redis", fr)
    yield fr
    fr.flushall()


def _run(coro):
    return asyncio.run(coro)


def test_save_and_load_roundtrip():
    params = {"space_code_list": ["SP0999999"], "online_search_switch": True}
    _run(save_custom_params("sid-1", params))
    assert _run(load_custom_params("sid-1")) == params


def test_load_missing_session_returns_none():
    assert _run(load_custom_params("nope")) is None


def test_save_overwrites_previous_params():
    _run(save_custom_params("sid-2", {"a": 1}))
    _run(save_custom_params("sid-2", {"b": 2}))
    assert _run(load_custom_params("sid-2")) == {"b": 2}


def test_contextvar_unchanged_set_get_reset():
    token = set_custom_params({"k": "v"})
    assert get_custom_params() == {"k": "v"}
    reset_custom_params(token)
    assert get_custom_params() == {}
