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
    attach_user_identity,
    build_auth_headers,
    get_resolved_auth,
    load_auth,
    reset_resolved_auth,
    resolve_auth_params,
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


def test_build_auth_headers_guip_token():
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="guip-token", guip_token="gp")
    )
    try:
        headers = build_auth_headers({})
        assert headers["guip-token"] == "gp"
        assert "guwp-token" not in headers
    finally:
        reset_resolved_auth(token)


def test_attach_user_identity_guip_user_mode():
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="guip-user", guip_user={"userId": "gu1"})
    )
    try:
        body = attach_user_identity({"REQ_BODY": {"param": {}}})
        assert body["REQ_BODY"]["guipUser"] == {"userId": "gu1"}
        assert "muwpUser" not in body["REQ_BODY"]
    finally:
        reset_resolved_auth(token)


def test_attach_muwp_user_alias_covers_guip_user():
    # attach_muwp_user 委托 attach_user_identity，guip-user 模式也应注入 guipUser
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="guip-user", guip_user={"userId": "gu2"})
    )
    try:
        body = attach_muwp_user({"REQ_BODY": {"param": {}}})
        assert body["REQ_BODY"]["guipUser"] == {"userId": "gu2"}
    finally:
        reset_resolved_auth(token)


def test_resolve_auth_params_priority_guip_token_before_muwp_user():
    # guip-token 优先级高于 muwp-user
    auth = resolve_auth_params(
        {"guip_token": "gt", "muwp_user": {"userId": "u"}}
    )
    assert auth.auth_mode == "guip-token"
    assert auth.guip_token == "gt"


def test_resolve_auth_params_guip_user_last():
    # guip-user 优先级最低（仅高于 none）
    auth = resolve_auth_params({"guip_user": {"userId": "gu"}})
    assert auth.auth_mode == "guip-user"
    assert auth.guip_user == {"userId": "gu"}


def test_resolve_auth_params_guip_user_below_muwp_user():
    auth = resolve_auth_params(
        {"muwp_user": {"userId": "m"}, "guip_user": {"userId": "g"}}
    )
    assert auth.auth_mode == "muwp-user"


def test_save_and_load_auth_guip_roundtrip():
    auth = ResolvedAuth(
        auth_mode="guip-user", guip_user={"userId": "gu-rt"}
    )
    _run(save_auth("sid-guip", auth))
    loaded = _run(load_auth("sid-guip"))
    assert loaded is not None
    assert loaded.auth_mode == "guip-user"
    assert loaded.guip_user == {"userId": "gu-rt"}
