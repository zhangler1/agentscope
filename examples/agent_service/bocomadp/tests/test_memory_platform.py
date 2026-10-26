# -*- coding: utf-8 -*-
"""platform SDK 门面测试（Task 9，§7.3）：生命周期 + 三接口判定。

三方法接收请求对象（构造经 mapping），本文件只测判定与生命周期。
"""
import asyncio
import sys
from pathlib import Path


import pytest
from euvd_memory_pysdk.models.base import SDKStandardResponse

import bocomadp.memory.platform as platform_mod
from bocomadp.memory.mapping import (
    build_register_request,
    build_save_request,
    build_search_request,
)
from bocomadp.memory.platform import (
    MemoriesSdkError,
    close_client,
    get_client,
    register_agent,
    save_memories,
    search_memory,
    start_client,
)
from bocomadp.memory.settings import MemorySettings
from bocomadp.memory.storage._models import MemoryAgentConfigRecord


def _record(**kw) -> MemoryAgentConfigRecord:
    return MemoryAgentConfigRecord(agent_id="a1", **kw)


def _resp(result, success=True, error="", tid="t1"):
    return SDKStandardResponse(success, result, error, tid)


class _FakeClient:
    """同步假 SDK 客户端：按方法名返回预置响应（或抛异常）。"""

    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    def register_agent(self, req):
        self.calls.append(("register", req))
        return self._mapping["register"](req)

    def search_memory(self, req):
        self.calls.append(("search", req))
        return self._mapping["search"](req)

    def save_memory_standard(self, req):
        self.calls.append(("save", req))
        return self._mapping["save"](req)

    def close(self):
        self._mapping.setdefault("closed", 0)
        self._mapping["closed"] = self._mapping.get("closed", 0) + 1


@pytest.fixture(autouse=True)
def _clean_client(monkeypatch):
    """每用例前重置模块级 _client；用例可再注入假客户端。"""
    monkeypatch.setattr(platform_mod, "_client", None)
    yield
    # monkeypatch 自动还原


def _install(monkeypatch, mapping):
    fake = _FakeClient(mapping)
    monkeypatch.setattr(platform_mod, "_client", fake)
    return fake


class TestLifecycle:
    def test_get_client_not_started(self):
        with pytest.raises(MemoriesSdkError):
            get_client()

    def test_close_client_idempotent(self):
        asyncio.run(close_client())  # 空转不抛
        asyncio.run(close_client())

    def test_start_client_overrides_old(self, monkeypatch):
        calls = {}
        closed = []

        class _Cfg:
            @staticmethod
            def from_env(environment=None, **overrides):
                calls["environment"] = environment
                calls["overrides"] = overrides
                return "cfg"

        class _Client:
            def __init__(self, cfg):
                self.cfg = cfg
                self.closed = False

            def close(self):
                closed.append(self)
                self.closed = True

        clients = []

        monkeypatch.setattr(platform_mod, "MemoriesClientConfig", _Cfg)
        monkeypatch.setattr(
            platform_mod, "MemoriesClient", lambda cfg: clients.append(_Client(cfg)) or clients[-1]
        )

        async def scenario():
            await start_client(MemorySettings(environment="sit"))
            c1 = get_client()
            assert c1 in clients
            await start_client(MemorySettings(environment="uat"))
            c2 = get_client()
            assert c2 is not c1  # 覆盖旧实例
            assert c1.closed     # 旧实例被关闭
            await close_client()
            assert c2.closed
            # 关闭后再取 → 抛
            try:
                get_client()
                raise AssertionError("should raise")
            except MemoriesSdkError:
                pass

        asyncio.run(scenario())
        assert calls["environment"] == "uat"  # 最后一次生效

    def test_client_cfg_overrides(self, monkeypatch):
        captured = {}

        class _Cfg:
            @staticmethod
            def from_env(environment=None, **overrides):
                captured["environment"] = environment
                captured.update(overrides)
                return "cfg"

        monkeypatch.setattr(platform_mod, "MemoriesClientConfig", _Cfg)
        s = MemorySettings(
            environment="sit",
            connect_timeout_seconds=5.0,
            read_timeout_seconds=20.0,
            headers={"X-A": "1"},
        )
        platform_mod._client_cfg(s)
        assert captured["environment"] == "sit"
        assert captured["connect_timeout"] == 5.0
        assert captured["read_timeout"] == 20.0
        assert captured["headers"] == {"X-A": "1"}


class TestRegister:
    def test_success_returns_caller(self, monkeypatch):
        fake = _install(monkeypatch, {
            "register": lambda req: _resp({"caller": "caller-xyz"}),
        })
        request = build_register_request(_record(), "智能体甲")

        async def scenario():
            got = await register_agent(request)
            assert got == "caller-xyz"

        asyncio.run(scenario())
        req = fake.calls[0][1]
        assert req is request            # 请求对象原样透传
        assert req.agent_id == "a1"
        assert req.agent_name == "智能体甲"

    def test_failed_raises(self, monkeypatch):
        _install(monkeypatch, {
            "register": lambda req: _resp(None, success=False, error="boom"),
        })
        request = build_register_request(_record(), "甲")
        with pytest.raises(MemoriesSdkError, match="boom"):
            asyncio.run(register_agent(request))

    def test_missing_caller_raises(self, monkeypatch):
        _install(monkeypatch, {
            "register": lambda req: _resp({"other": 1}),
        })
        request = build_register_request(_record(), "甲")
        with pytest.raises(MemoriesSdkError, match="caller"):
            asyncio.run(register_agent(request))


class TestSearch:
    def test_success_list(self, monkeypatch):
        items = [{"memory_id": "m1", "content": "x"}]
        fake = _install(monkeypatch, {
            "search": lambda req: _resp(items),
        })
        request = build_search_request(_record(top_k=3), "u1", "查询")

        async def scenario():
            got = await search_memory(request)
            assert got == items

        asyncio.run(scenario())
        req = fake.calls[0][1]
        assert req is request
        assert req.keyword == "查询"
        assert req.user_code == "u1"
        assert req.top_k == 3

    def test_failed_raises(self, monkeypatch):
        _install(monkeypatch, {
            "search": lambda req: _resp(None, success=False, error="timeout"),
        })
        request = build_search_request(_record(), "u1", "q")
        with pytest.raises(MemoriesSdkError, match="timeout"):
            asyncio.run(search_memory(request))


class TestSave:
    def test_success(self, monkeypatch):
        fake = _install(monkeypatch, {
            "save": lambda req: _resp("success"),
        })
        request = build_save_request(
            _record(), "u1", [("user", "content")], "s1"
        )

        async def scenario():
            await save_memories(request)

        asyncio.run(scenario())
        req = fake.calls[0][1]
        assert req is request
        assert req.user_code == "u1"
        assert req.run_id == "s1"
        assert req.messages[0].content == "content"

    def test_result_not_success_raises(self, monkeypatch):
        _install(monkeypatch, {
            "save": lambda req: _resp("partial"),
        })
        request = build_save_request(_record(), "u1", [("user", "c")], "s1")
        with pytest.raises(MemoriesSdkError, match="success"):
            asyncio.run(save_memories(request))

    def test_failed_raises(self, monkeypatch):
        _install(monkeypatch, {
            "save": lambda req: _resp(None, success=False, error="http 500"),
        })
        request = build_save_request(_record(), "u1", [("user", "c")], "s1")
        with pytest.raises(MemoriesSdkError, match="500"):
            asyncio.run(save_memories(request))
