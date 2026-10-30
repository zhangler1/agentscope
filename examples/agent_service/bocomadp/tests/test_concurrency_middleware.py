# -*- coding: utf-8 -*-
"""Tests for ConcurrencyGuardMiddleware (ASGI layer)."""
from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from bocomadp.concurrency.guard import ConcurrencyGuard
from bocomadp.middleware.concurrency_guard import ConcurrencyGuardMiddleware

_THROTTLED_GLOBAL = {
    "status": "throttled",
    "session_id": "",
    "reason": "global",
    "message": "系统繁忙，请稍后再试",
}
_THROTTLED_USER = {
    "status": "throttled",
    "session_id": "",
    "reason": "user",
    "message": "您同时进行的对话已达上限，请稍后再试",
}


def _build_app(fake_redis, *, max_running=2, max_running_per_user=1):
    guard = ConcurrencyGuard(lambda: fake_redis, max_running=max_running,
                             max_running_per_user=max_running_per_user,
                             watch_interval=0.01, watch_timeout=0.1)

    async def chat(request):
        return JSONResponse({"status": "started", "session_id": "sid-1"})

    async def other(request):
        return JSONResponse({"ok": True})

    routes = [Route("/chat/", chat, methods=["POST"]),
              Route("/other", other, methods=["GET"])]
    app = Starlette(routes=routes)
    app.add_middleware(ConcurrencyGuardMiddleware, guard=guard)
    return TestClient(app), guard


def test_throttled_when_global_full(fake_redis):
    client, guard = _build_app(fake_redis, max_running=1)
    assert client.post("/chat/", json={"session_id": "s1"}, headers={"X-User-ID": "a"}).status_code == 200
    resp = client.post("/chat/", json={"session_id": "s2"}, headers={"X-User-ID": "b"})
    assert resp.status_code == 200
    assert resp.json() == _THROTTLED_GLOBAL


def test_throttled_when_user_full(fake_redis):
    client, guard = _build_app(fake_redis, max_running_per_user=1)
    assert client.post("/chat/", json={}, headers={"X-User-ID": "a"}).status_code == 200
    resp = client.post("/chat/", json={}, headers={"X-User-ID": "a"})
    assert resp.json() == _THROTTLED_USER
    # 其他用户放行
    assert client.post("/chat/", json={}, headers={"X-User-ID": "b"}).status_code == 200


def test_mounted_api_prefix_is_guarded(fake_redis):
    """app 被 mount("/api") 时真实路径是 /api/chat/，闸门必须仍然生效。"""
    client, guard = _build_app(fake_redis, max_running=1)
    app = client.app

    async def _call(path, root_path):
        """按裸 ASGI 构造 scope，模拟挂载前缀下的路径。"""
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"x-user-id", b"a")],
        }
        if root_path is not None:
            scope["root_path"] = root_path

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return sent, body

    import asyncio

    async def _scenario():
        # 第一次：占用名额 → 放行
        _sent, body = await _call("/api/chat/", "/api")
        assert json.loads(body)["status"] == "started"
        # 第二次：超出上限 → 命中闸门（带 root_path，剥前缀后等于 /chat/）
        _sent, body = await _call("/api/chat/", "/api")
        assert json.loads(body)["status"] == "throttled"
        # 无 root_path：路径尾部仍是 /chat/ → 兜底同样命中闸门
        _sent, body = await _call("/api/chat/", None)
        assert json.loads(body)["status"] == "throttled"

    asyncio.run(_scenario())


def test_other_paths_untouched(fake_redis):
    client, _ = _build_app(fake_redis)
    resp = client.get("/other")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_missing_user_header_passes_through(fake_redis):
    client, _ = _build_app(fake_redis)
    resp = client.post("/chat/", json={})  # 无 X-User-ID
    assert resp.status_code == 200          # 框架侧才会 401,中间件不拦


def test_non_2xx_response_rolls_back(fake_redis):
    import asyncio

    async def _scenario() -> None:
        guard = ConcurrencyGuard(lambda: fake_redis, max_running=1,
                                 max_running_per_user=1)
        # 入口占位由中间件 __call__ 的 try_acquire 完成

        class _FailApp:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 409,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b"{}",
                            "more_body": False})

        mw = ConcurrencyGuardMiddleware(_FailApp(), guard)
        # 构造最小 HTTP scope,直调中间件(跳过 Starlette,纯 ASGI)
        scope = {"type": "http", "method": "POST", "path": "/chat/",
                 "headers": [(b"x-user-id", b"a")]}
        received = []
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)

        assert sent[0]["status"] == 409      # 下游响应原样透传
        assert await guard.try_acquire("a") is True  # 名额已回滚

    asyncio.run(_scenario())


def test_reconcile_before_acquire_on_request(fake_redis):
    import asyncio

    async def _scenario() -> None:
        calls: list[str] = []
        real = ConcurrencyGuard(lambda: fake_redis, max_running=2,
                                max_running_per_user=1)

        class _SpyGuard:
            async def reconcile_on_startup(self) -> None:
                calls.append("reconcile_on_startup")
                await real.reconcile_on_startup()

            async def reconcile(self, grace_secs: float = 0.0) -> int:
                calls.append("reconcile")
                return await real.reconcile(grace_secs)

            async def try_acquire_ex(self, user_id: str, agent_id: str = "",
                                     *, base_dims: bool = True):
                calls.append("try_acquire_ex")
                return await real.try_acquire_ex(user_id, agent_id,
                                                 base_dims=base_dims)

            async def reserve(self, session_id: str) -> None:
                calls.append("reserve")
                await real.reserve(session_id)

            async def register(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("register")
                await real.register(session_id, user_id, agent_id)

            async def rollback(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("rollback")
                await real.rollback(session_id, user_id, agent_id)

        class _OkApp:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"status": "started", "session_id": "sid-1"}',
                            "more_body": False})

        mw = ConcurrencyGuardMiddleware(_OkApp(), _SpyGuard())
        scope = {"type": "http", "method": "POST", "path": "/chat/",
                 "headers": [(b"x-user-id", b"a")]}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)

        # 启动对账仅首次执行;每次请求:先 reconcile 再 try_acquire_ex
        assert calls[0] == "reconcile_on_startup"
        assert calls.index("reconcile") < calls.index("try_acquire_ex")
        assert calls.count("reconcile_on_startup") == 1
        assert calls.count("reconcile") == 1
        assert calls.count("try_acquire_ex") == 1
        assert calls[-1] == "register"   # 2xx → 注册(值带 token,见 guard 测试)

        # 再次请求:启动对账不重复执行,仍先 reconcile 后 try_acquire
        sent.clear()
        await mw(scope, receive, send)
        assert calls.count("reconcile_on_startup") == 1
        assert calls.count("reconcile") == 2

    asyncio.run(_scenario())


def test_registered_value_carries_token(fake_redis):
    import asyncio

    async def _scenario() -> None:
        guard = ConcurrencyGuard(lambda: fake_redis, max_running=2,
                                 max_running_per_user=1)

        class _OkApp:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"status": "started", "session_id": "sid-1"}',
                            "more_body": False})

        mw = ConcurrencyGuardMiddleware(_OkApp(), guard)
        scope = {"type": "http", "method": "POST", "path": "/chat/",
                 "headers": [(b"x-user-id", b"a")]}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)

        entries = await fake_redis.hgetall("agentscope:running:sessions")
        assert "sid-1" in entries
        value = entries["sid-1"]
        assert value.startswith("a:")
        assert value.count(":") == 2   # a:<ts>:<token>

    asyncio.run(_scenario())


def test_reserve_called_after_successful_acquire(fake_redis):
    """占位成功 → 记"待转正"（run 可能先结束，登记阶段据此退占位）。"""
    import asyncio

    async def _scenario() -> None:
        calls: list[str] = []
        real = ConcurrencyGuard(lambda: fake_redis, max_running=2,
                                max_running_per_user=1)

        class _SpyGuard:
            async def reconcile_on_startup(self) -> None:
                await real.reconcile_on_startup()

            async def reconcile(self, grace_secs: float = 0.0) -> int:
                return await real.reconcile(grace_secs)

            async def try_acquire_ex(self, user_id: str, agent_id: str = "",
                                     *, base_dims: bool = True):
                calls.append("try_acquire_ex")
                return await real.try_acquire_ex(user_id, agent_id,
                                                 base_dims=base_dims)

            async def reserve(self, session_id: str) -> None:
                calls.append(f"reserve:{session_id}")
                await real.reserve(session_id)

            async def register(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("register")
                await real.register(session_id, user_id, agent_id)

            async def rollback(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("rollback")
                await real.rollback(session_id, user_id, agent_id)

        class _OkApp:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"status": "started", "session_id": "sid-1"}',
                            "more_body": False})

        mw = ConcurrencyGuardMiddleware(_OkApp(), _SpyGuard())
        scope = {"type": "http", "method": "POST", "path": "/chat/",
                 "headers": [(b"x-user-id", b"a")]}
        sent = []

        async def receive():
            return {"type": "http.request",
                    "body": b'{"session_id": "sid-1"}', "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)

        assert "reserve:sid-1" in calls
        # 先占位、再记待转正、最后登记
        assert calls.index("try_acquire_ex") < calls.index("reserve:sid-1")
        assert calls.index("reserve:sid-1") < calls.index("register")

    asyncio.run(_scenario())


def test_disabled_skips_all_guard_calls(fake_redis):
    import asyncio

    async def _scenario() -> None:
        calls: list[str] = []
        real = ConcurrencyGuard(lambda: fake_redis, max_running=2,
                                max_running_per_user=1)

        class _SpyGuard:
            async def reconcile_on_startup(self) -> None:
                calls.append("reconcile_on_startup")
                await real.reconcile_on_startup()

            async def reconcile(self, grace_secs: float = 0.0) -> int:
                calls.append("reconcile")
                return await real.reconcile(grace_secs)

            async def try_acquire_ex(self, user_id: str, agent_id: str = "",
                                     *, base_dims: bool = True):
                calls.append("try_acquire_ex")
                return await real.try_acquire_ex(user_id, agent_id,
                                                 base_dims=base_dims)

            async def reserve(self, session_id: str) -> None:
                calls.append("reserve")
                await real.reserve(session_id)

            async def register(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("register")
                await real.register(session_id, user_id, agent_id)

            async def rollback(self, session_id: str, user_id: str,
                               agent_id: str = "") -> None:
                calls.append("rollback")
                await real.rollback(session_id, user_id, agent_id)

        class _OkApp:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"status": "started", "session_id": "sid-1"}',
                            "more_body": False})

        # enabled=False 且无智能体配置:完全透传,不执行对账/占位/注册
        mw = ConcurrencyGuardMiddleware(_OkApp(), _SpyGuard(), enabled=False,
                                        has_agent_config=lambda: False)
        scope = {"type": "http", "method": "POST", "path": "/chat/",
                 "headers": [(b"x-user-id", b"a")]}
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await mw(scope, receive, send)

        assert sent[0]["status"] == 200          # 请求透传成功
        assert calls == []                        # spy 上 3 个方法均未被调用

    asyncio.run(_scenario())
