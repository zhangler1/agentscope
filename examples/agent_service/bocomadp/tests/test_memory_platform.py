# -*- coding: utf-8 -*-
"""memory/platform.py 平台客户端单测：本地 http.server 假平台。

用 monkeypatch 替换 ``pf._get_url``（接口名 → 本地假平台 URL），
不依赖 config.yaml 的 memory 节点（Task 6 才加入 AppConfig）。
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from bocomadp.memory import platform as pf

# 接口名 → 平台契约路径（与 docs/记忆.txt 一致）
_ENDPOINTS = {
    "register": "/registerAgent.do",
    "retrieve": "/searchMemory.do",
    "extract": "/saveMemoriesStandard.do",
}


class _Fake(BaseHTTPRequestHandler):
    """极简假平台：按路径返回固定响应，记录最近一次 param。"""

    protocol_version = "HTTP/1.1"
    last_param = {}

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        envelope = json.loads(parse_qs(raw)["REQ_MESSAGE"][0])
        _Fake.last_param = envelope["REQ_BODY"]["param"]
        if self.path.endswith("/registerAgent.do"):
            result = {"caller": "c1", "agentId": "a", "agentName": "", "agentPlat": 0}
        elif self.path.endswith("/searchMemory.do"):
            result = [{"memory": "m1", "score": 0.9, "id": "i1"}]
        else:
            result = "success"
        body = json.dumps(
            {"RSP_BODY": {"result": result}, "RSP_HEAD": {"TRAN_SUCCESS": "1"}},
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_server(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(
        pf,
        "_get_url",
        lambda name: f"http://127.0.0.1:{port}{_ENDPOINTS[name]}",
    )
    yield
    srv.shutdown()
    srv.server_close()


def _run(coro):
    return asyncio.run(coro)


def test_register_returns_caller(fake_server):
    caller = _run(pf.register_agent({"agentId": "a"}))
    assert caller == "c1"
    assert _Fake.last_param == {"agentId": "a"}


def test_search_returns_list(fake_server):
    res = _run(pf.search_memory({"keyword": "k"}))
    assert res[0]["memory"] == "m1"


def test_save_success(fake_server):
    _run(pf.save_memories({"messages": [{"role": "user", "content": "hi"}]}))


def test_post_form_logs_raw_request_and_response(fake_server, caplog):
    """DEBUG 级别输出请求 envelope 与响应原始报文字符串。"""
    import logging as _logging

    with caplog.at_level(_logging.DEBUG, logger="as"):
        caller = _run(pf.register_agent({"agentId": "a"}))
    assert caller == "c1"
    messages = [record.message for record in caplog.records]
    assert any(
        "memory: platform request url=" in m and '"agentId": "a"' in m
        for m in messages
    )
    assert any(
        "memory: platform response url=" in m
        and '"caller": "c1"' in m
        and "RSP_HEAD" in m
        and "TRAN_SUCCESS" in m
        for m in messages
    )
