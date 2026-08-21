# -*- coding: utf-8 -*-
"""raw_request 工具测试（mock httpx.AsyncClient，不发真实网络）。

覆盖：
- URL 拼接（api_url.rstrip('/') + 接口路径）
- 认证头注入（build_auth_headers 底座）
- externalDataQuery 参数注入（systemCode / businessType / requestCause）
- muwp-user 模式 body 注入
- 舆情 yq_info 附加情感代码
- 无效 intent / 无效报文 / 超时 / HTTP 错误 / 无效响应 JSON → 友好错误
- 挂载在 enterprise.py 中且名称 = raw_request_tool
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bocomadp.config import base as _base
from bocomadp.deerflow.auth_context import (
    ResolvedAuth,
    reset_resolved_auth,
    set_resolved_auth,
)
from bocomadp.deerflow.custom_params import (
    reset_custom_params,
    set_custom_params,
)
from bocomadp.tools.enterprise import build_enterprise_tools
from bocomadp.tools.raw_request import DEFAULT_API_PATHS, _raw_request_tool_impl


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {"raw_request": {"api_url": "http://gw/raw.do", "timeout": 30}},
    )


class _FakeResponse:
    def __init__(self, payload, text: str | None = None, json_raises: bool = False):
        self._payload = payload
        self._text = text
        self._json_raises = json_raises

    def raise_for_status(self):
        return None

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        return json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if self._json_raises:
            raise ValueError("invalid json")
        return self._payload


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.last_kwargs: dict | None = None
        self.payload = {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {"result": [{"name": "测试企业"}]},
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.last_kwargs = {"url": url, **kwargs}
        return _FakeResponse(self.payload)


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    return client


def _run(body: str, intent: str) -> str:
    token_params = set_custom_params(
        {
            "tools_param": {
                "externalDataQuery": {
                    "systemCode": "sys1",
                    "businessType": "bt1",
                    "requestCause": "rc1",
                }
            }
        }
    )
    token_auth = set_resolved_auth(
        ResolvedAuth(auth_mode="guwp-token", guwp_token="tok-1")
    )
    try:
        return asyncio.run(_raw_request_tool_impl(body, intent))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token_params)


# ---------- 正常路径 ----------


def test_url_and_body_injection(fake_client):
    out = _run(
        json.dumps({"REQ_HEAD": {}, "REQ_BODY": {"param": {"company": "X"}}}),
        "enterprise_detail",
    )
    assert fake_client.last_kwargs is not None
    # URL = api_url.rstrip('/') + 接口路径
    assert (
        fake_client.last_kwargs["url"]
        == "http://gw/raw.do" + DEFAULT_API_PATHS["enterprise_detail"]
    )
    body = fake_client.last_kwargs["json"]
    # externalDataQuery 参数注入
    assert body["REQ_BODY"]["param"]["sysCode"] == "sys1"
    assert body["REQ_BODY"]["param"]["businessType"] == "bt1"
    assert body["REQ_BODY"]["param"]["requestCause"] == "rc1"
    # 认证头注入（guwp-token 模式）
    assert fake_client.last_kwargs["headers"]["guwp-token"] == "tok-1"
    # 响应原样 JSON 返回
    parsed = json.loads(out)
    assert parsed["RSP_HEAD"]["TRAN_SUCCESS"] == "1"


def test_yq_info_adds_emotion_codes(fake_client):
    _run(json.dumps({"REQ_HEAD": {}, "REQ_BODY": {"param": {}}}), "yq_info")
    body = fake_client.last_kwargs["json"]
    assert body["REQ_BODY"]["param"]["newsEmotionCode"] == "0,1,2"
    assert body["REQ_BODY"]["param"]["entEmotionCode"] == "0,1,2"


def test_muwp_user_attached(fake_client):
    token_params = set_custom_params({})
    token_auth = set_resolved_auth(
        ResolvedAuth(auth_mode="muwp-user", muwp_user={"userId": "u1"})
    )
    try:
        asyncio.run(_raw_request_tool_impl("{}", "enterprise_detail"))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token_params)
    body = fake_client.last_kwargs["json"]
    assert body["REQ_BODY"]["muwpUser"] == {"userId": "u1"}


def test_without_tools_param_ok(fake_client):
    # custom_params 没有 externalDataQuery 时也不报错（参数注入跳过）
    token_params = set_custom_params({})
    token_auth = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        out = asyncio.run(_raw_request_tool_impl("{}", "enterprise_detail"))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token_params)
    assert "error" not in json.loads(out)


# ---------- 异常路径（全部转友好错误，不断线） ----------


def test_invalid_intent_returns_error():
    out = _run("{}", "not_exist")
    parsed = json.loads(out)
    assert "error" in parsed[0]
    assert "无效的接口标识" in parsed[0]["error"]


def test_invalid_json_body_returns_error():
    out = _run("{not json", "enterprise_detail")
    parsed = json.loads(out)
    assert "error" in parsed[0]
    assert "请求报文格式错误" in parsed[0]["error"]


def test_timeout_returns_error(monkeypatch):
    class _TimeoutClient(_FakeClient):
        async def post(self, url, **kwargs):
            raise httpx.TimeoutException("timeout")

    client = _TimeoutClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    out = _run("{}", "enterprise_detail")
    parsed = json.loads(out)
    assert parsed[0]["error"] == "请求超时。"


def test_http_error_returns_error(monkeypatch):
    class _ErrClient(_FakeClient):
        async def post(self, url, **kwargs):
            request = httpx.Request("POST", url)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError(
                "500 Server Error", request=request, response=response
            )

    client = _ErrClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    out = _run("{}", "enterprise_detail")
    parsed = json.loads(out)
    assert "请求失败" in parsed[0]["error"]


def test_invalid_response_json_returns_error(monkeypatch):
    class _BadRespClient(_FakeClient):
        async def post(self, url, **kwargs):
            self.last_kwargs = {"url": url, **kwargs}
            return _FakeResponse(payload=None, json_raises=True)

    client = _BadRespClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    out = _run("{}", "enterprise_detail")
    parsed = json.loads(out)
    assert "无效的JSON" in parsed[0]["error"]


# ---------- enterprise.py 挂载 ----------


def test_raw_request_mounted_in_enterprise(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {
            "raw_request": {"api_url": "http://gw/raw.do", "timeout": 30},
            "online_search": {"api_url": "http://x", "timeout": 30, "max_results": 5},
        },
    )
    token_params = set_custom_params({})
    try:
        tools = asyncio.run(build_enterprise_tools("u1", "a1", "s1"))
        names = [t.name for t in tools]
        assert "外数查" in names
        assert "cross_search" in names
        assert "online_search" not in names  # 默认不挂联网搜索
    finally:
        reset_custom_params(token_params)
