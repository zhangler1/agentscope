# -*- coding: utf-8 -*-
"""online_search 工具测试（mock httpx，不发真实网络）。"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from bocomadp.config import base as _base
from bocomadp.deerflow.auth_context import (
    ResolvedAuth,
    reset_resolved_auth,
    set_resolved_auth,
)
from bocomadp.tools.online_search import search_online_backend


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {
            "online_search": {
                "api_url": "http://gw/querySources.do",
                "timeout": 30,
                "max_results": 2,
            }
        },
    )


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    @property
    def text(self):
        return json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeClient:
    """记录请求参数并返回固定 payload 的 httpx.AsyncClient 桩。"""

    def __init__(self, *args, **kwargs):
        self.last_kwargs = None
        self.payload = {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {
                "result": [
                    {"title": "B", "content": "b", "score": "0.5"},
                    {"title": "A", "content": "a", "score": "1.0"},
                ]
            },
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


def test_request_body_and_repository(fake_client):
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        asyncio.run(search_online_backend("测试问题"))
    finally:
        reset_resolved_auth(token)
    body = fake_client.last_kwargs["json"]
    assert fake_client.last_kwargs["url"] == "http://gw/querySources.do"
    assert body["REQ_BODY"]["param"]["summaryQuestion"] == "测试问题"
    assert body["REQ_BODY"]["param"]["repository"] == "online-search"
    assert body["REQ_BODY"]["param"]["param"] == {"channelId": "0"}


def test_auth_header_injected(fake_client):
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="guwp-token", guwp_token="g-tok")
    )
    try:
        asyncio.run(search_online_backend("q"))
    finally:
        reset_resolved_auth(token)
    headers = fake_client.last_kwargs["headers"]
    assert headers["guwp-token"] == "g-tok"


def test_results_sorted_and_truncated(fake_client):
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        results = asyncio.run(search_online_backend("q"))
    finally:
        reset_resolved_auth(token)
    assert len(results) == 2                      # max_results=2
    assert results[0]["title"] == "A"             # score 1.0 排前
    assert results[1]["title"] == "B"


def test_trans_success_not_1_logs_raw_payload_debug(monkeypatch, caplog):
    class _FailClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.payload = {
                "RSP_HEAD": {"TRAN_SUCCESS": "0", "PROCESS_STATUS_CODE": "E1"},
                "RSP_BODY": {"result": []},
            }

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FailClient())
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        with caplog.at_level(logging.DEBUG):
            results = asyncio.run(search_online_backend("q"))
    finally:
        reset_resolved_auth(token)
    assert results == []
    assert "TRAN_SUCCESS" in caplog.text          # debug 打印了原始 payload
