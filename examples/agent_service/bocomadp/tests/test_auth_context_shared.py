# -*- coding: utf-8 -*-
"""auth_context 扩展测试：存储委托 + 三工具共享的鉴权纯函数。"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from bocomadp.deerflow import _session_store
from bocomadp.deerflow.auth_context import (
    ResolvedAuth,
    attach_muwp_user,
    build_auth_headers,
    get_resolved_auth,
    load_auth,
    reset_resolved_auth,
    save_auth,
    set_resolved_auth,
)


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fr = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(_session_store, "_redis", fr)
    yield fr
    fr.flushall()


def _run(coro):
    return asyncio.run(coro)


def test_save_and_load_auth_roundtrip():
    auth = ResolvedAuth(auth_mode="guwp-token", guwp_token="tok-1")
    _run(save_auth("sid-1", auth))
    assert _run(load_auth("sid-1")) == auth


def test_load_auth_missing_returns_none():
    assert _run(load_auth("nope")) is None


def test_build_auth_headers_guwp():
    token = set_resolved_auth(ResolvedAuth(auth_mode="guwp-token", guwp_token="g"))
    try:
        headers = build_auth_headers({"Content-Type": "application/json"})
        assert headers["guwp-token"] == "g"
        assert "jrt-auth-code" not in headers
    finally:
        reset_resolved_auth(token)


def test_build_auth_headers_okic_adds_type():
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="okic-token", okic_token="o", okic_type="t")
    )
    try:
        headers = build_auth_headers({})
        assert headers["okic-token"] == "o"
        assert headers["okic-type"] == "t"
    finally:
        reset_resolved_auth(token)


def test_build_auth_headers_none_mode_no_op():
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        assert build_auth_headers({"Accept": "*/*"}) == {"Accept": "*/*"}
    finally:
        reset_resolved_auth(token)


def test_attach_muwp_user_only_in_muwp_mode():
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="muwp-user", muwp_user={"userId": "u1"})
    )
    try:
        body = attach_muwp_user({"REQ_BODY": {"param": {}}})
        assert body["REQ_BODY"]["muwpUser"] == {"userId": "u1"}
    finally:
        reset_resolved_auth(token)

    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        body = attach_muwp_user({"REQ_BODY": {"param": {}}})
        assert "muwpUser" not in body["REQ_BODY"]
    finally:
        reset_resolved_auth(token)
