# -*- coding: utf-8 -*-
"""personal_search 工具测试（mock httpx，不发真实网络）。"""
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
from bocomadp.tools.personal_search import search_personal_backend


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {
            "personal_search": {
                "api_url": "http://gw/personal.do",
                "timeout": 30,
                "source_type": "WDZS",
                "repository": "personal-search",
                "search_type": "0",
                "headers": {"jumpCloud-Env": "BASE"},
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
    def __init__(self, *args, **kwargs):
        self.last_kwargs = None
        self.payload = {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {
                "result": [
                    {
                        "title": "T1",
                        "content": "c1",
                        "docId": "d1",
                        "score": "0.9",
                        "repository": "personal-search",
                        "url": "http://u",
                        "sourceType": "WDZS",
                        "question": "q",
                    }
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


def test_multipart_req_message(fake_client):
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        asyncio.run(
            search_personal_backend("问题", space_code_id="PER1", space_code=["C1"])
        )
    finally:
        reset_resolved_auth(token)
    assert fake_client.last_kwargs["url"] == "http://gw/personal.do"
    req_message = fake_client.last_kwargs["data"]["REQ_MESSAGE"][1]
    body = json.loads(req_message)
    param = body["REQ_BODY"]["param"]
    assert param["summaryQuestion"] == "问题"
    assert param["sourceType"] == "WDZS"
    assert param["repository"] == "personal-search"
    assert param["searchType"] == "0"
    assert param["param"] == {
        "psnlSpaceCodeId": "PER1",
        "psnlCategoryIdList": ["C1"],
    }


def test_no_space_params_omits_inner_param(fake_client):
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        asyncio.run(search_personal_backend("问题"))
    finally:
        reset_resolved_auth(token)
    req_message = fake_client.last_kwargs["data"]["REQ_MESSAGE"][1]
    body = json.loads(req_message)
    assert "param" not in body["REQ_BODY"]["param"]   # 无空间参数时不加内层


def test_muwp_user_attached_in_muwp_mode(fake_client):
    token = set_resolved_auth(
        ResolvedAuth(auth_mode="muwp-user", muwp_user={"userId": "u1"})
    )
    try:
        asyncio.run(search_personal_backend("q"))
    finally:
        reset_resolved_auth(token)
    req_message = fake_client.last_kwargs["data"]["REQ_MESSAGE"][1]
    body = json.loads(req_message)
    assert body["REQ_BODY"]["muwpUser"] == {"userId": "u1"}


def test_empty_results_returns_info(fake_client, monkeypatch):
    class _EmptyClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.payload = {
                "RSP_HEAD": {"TRAN_SUCCESS": "1"},
                "RSP_BODY": {"result": []},
            }

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _EmptyClient())
    token = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        out = asyncio.run(search_personal_backend("没找到的词"))
    finally:
        reset_resolved_auth(token)
    assert "未找到相关内容" in out
