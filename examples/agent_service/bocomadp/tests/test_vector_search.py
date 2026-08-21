# -*- coding: utf-8 -*-
"""vector_search 工具测试（mock httpx，不发真实网络）。"""
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
from bocomadp.tools.vector_search import search_vector_backend


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {
            "vector_search": {
                "api_url": "http://gw/vector.do",
                "timeout": 30,
                "page_size": 10,
                "text_top_n": 7,
                "vector_top_n": 10,
                "space_codes": ["SP0999999"],
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
                        "docGuid": "g1",
                        "score": "0.8",
                        "repository": "agg",
                        "question": "q",
                        "sourceType": "1",
                        "knowType": "2",
                        "createTime": "2026-01-01",
                        "updateTime": "2026-01-02",
                        "hobbies": ["h1"],
                        "fullCategoryName": ["c1"],
                        "orgId": "o1",
                        "fullOrgName": "org",
                        "knowStatus": "3",
                        "attachEcmId": "e1",
                        "fromAttachment": False,
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


def test_request_body_from_source_param(fake_client):
    token_params = set_custom_params(
        {
            "tools_param": {
                "source_param": {
                    "sourceType": "1",
                    "repository": "agg-repo",
                    "aggRepositories": ["a", "b"],
                    "HNSSParam": {"textTopN": 7},
                }
            }
        }
    )
    token_auth = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        asyncio.run(search_vector_backend("关键词"))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token_params)
    body = fake_client.last_kwargs["json"]
    assert fake_client.last_kwargs["url"] == "http://gw/vector.do"
    param = body["REQ_BODY"]["param"]
    assert param["summaryQuestion"] == "关键词"
    assert param["sourceType"] == "1"
    assert param["repository"] == "agg-repo"
    assert param["aggRepositories"] == ["a", "b"]
    assert param["param"] == {"textTopN": 7}
    assert body["REQ_HEAD"] == {"TRANS_PROCESS": "", "TRAN_ID": ""}


def test_source_param_missing_returns_clear_error(monkeypatch):
    class _NoCallClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.called = False

        async def post(self, url, **kwargs):
            self.called = True
            return _FakeResponse({})

    client = _NoCallClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    token = set_custom_params({})            # 无 tools_param.source_param
    token_auth = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        out = asyncio.run(search_vector_backend("q"))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token)
    assert "source_param" in out            # 显式错误信息
    assert client.called is False           # 未发请求


def test_results_19_fields(fake_client):
    token_params = set_custom_params(
        {"tools_param": {"source_param": {"sourceType": "1"}}}
    )
    token_auth = set_resolved_auth(ResolvedAuth(auth_mode="none"))
    try:
        out = asyncio.run(search_vector_backend("q"))
    finally:
        reset_resolved_auth(token_auth)
        reset_custom_params(token_params)
    parsed = json.loads(out)
    entry = parsed[0]
    for key in (
        "title", "url", "docId", "score", "repository", "content",
        "docGuid", "question", "sourceType", "knowType", "createTime",
        "updateTime", "hobbies", "fullCategoryName", "orgId",
        "fullOrgName", "knowStatus", "attachEcmId", "fromAttachment",
    ):
        assert key in entry
