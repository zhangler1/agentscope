# -*- coding: utf-8 -*-
"""/chat 并发控制 ASGI 中间件(入口对账 + 三维占位 + 响应回滚/注册)。

请求方向:
- 首次请求执行一次 ``reconcile_on_startup``(重建计数,吸收漂移;
  框架 create_app 使用自定义 lifespan,@app.on_event 不执行,故惰性到
  首个请求时执行,此时事件循环已运行);
- 每次请求先 ``reconcile(grace_secs)``(入口对账,无限频)再 ``try_acquire_ex``;
- ``agent_id`` 在 JSON 请求体里而 ASGI 的 ``receive`` 只能消费一次:
  首次 receive 聚合完整 body(/chat/ 为 KB 级 JSON,缓存无压力),
  json.loads 取 agent_id 与 session_id(解析失败视为无 → 跳过 agent 维度),
  再向下游提供回灌式 receive(先回放缓存 body,后续消息透传);
- 占位成功后记"待转正"(``reserve``),登记在响应之后(``register``):
  两步之间 run 可能已经结束(run 秒失败),``release_session`` 先到会记
  提前结束标记,``register`` 见到标记即退掉占位、不再登记;
- 启停判定:``enabled``(平台总开关,管用户/全局两维)与智能体配置
  (``has_agent_config`` 标志)任一激活,闸机才运转;两者皆无 → 零开销透传。
响应方向:
- 2xx → 从响应体解析 session_id 并登记(值带 user:ts:token + 伴随哈希
  sid→agent_id);
- 非 2xx / 抛异常 → 回滚本次全部已占名额(防止无效请求把名额占成死数)。
被拒时返回 ``200 + {"status":"throttled", "reason", "message"}``,reason
标明维度(user / agent / global),message 为可直接展示的中文文案。
任何 Redis 异常 → fail-open 放行(记日志),绝不阻断正常请求。
"""
from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("bocomadp.concurrency.guard")

Scope = dict
Message = dict
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_CHAT_PATH = "/chat/"


def _is_chat_request(scope: Scope) -> bool:
    """判断是否"发消息"请求（POST 聊天入口）。

    整个 app 被 ``root_app.mount("/api", app)`` 挂载，ASGI 传入的
    ``scope["path"]`` 是**完整路径**（"/api/chat/"），``scope["root_path"]``
    是挂载前缀（"/api"）；这里先剥前缀再比较，剥不掉时按路径尾部兜底匹配。
    直接拿 ``scope["path"]`` 与 "/chat/" 比较会永远不成立（闸门空转）。
    """
    path = scope.get("path", "") or ""
    root = (scope.get("root_path", "") or "").rstrip("/")
    rel = path[len(root):] if root and path.startswith(root) else path
    return rel == _CHAT_PATH or path.endswith(_CHAT_PATH)

# 各维度限流文案(前端可直接展示)
_THROTTLED_MESSAGES = {
    "agent": "该智能体同时进行的对话已达上限，请稍后再试",
    "user": "您同时进行的对话已达上限，请稍后再试",
    "global": "系统繁忙，请稍后再试",
}
_FALLBACK_MESSAGE = _THROTTLED_MESSAGES["global"]


def _throttled_body(reason: str) -> bytes:
    payload = {
        "status": "throttled",
        "session_id": "",
        "reason": reason,
        "message": _THROTTLED_MESSAGES.get(reason, _FALLBACK_MESSAGE),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ConcurrencyGuardMiddleware:
    """Gate POST /chat/ with per-user / per-agent / global concurrency limits."""

    def __init__(
        self,
        app: ASGIApp,
        guard,
        *,
        enabled: bool = True,
        has_agent_config: Callable[[], bool] | None = None,
        grace_secs: float = 6.0,
    ) -> None:
        self.app = app
        self._guard = guard
        # 平台总开关:只管用户/全局两维;智能体维度由配置存在性独立驱动
        self._enabled = enabled
        self._has_agent_config = has_agent_config
        self._grace_secs = grace_secs
        self._startup_reconciled = False

    async def _reconcile_on_startup(self) -> None:
        """启动对账惰性执行;成功才置位,失败保留重试机会。"""
        if not self._startup_reconciled:
            try:
                await self._guard.reconcile_on_startup()
            except Exception:  # noqa: BLE001 — fail-open:放行请求,下次请求重试
                logger.exception("concurrency guard startup reconcile failed; fail-open (will retry)")
                return
            self._startup_reconciled = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not _is_chat_request(scope)
        ):
            await self.app(scope, receive, send)
            return

        # 闸机整体空转(两维开关均未激活)→ 零开销透传
        if not self._enabled and (
            self._has_agent_config is None or not self._has_agent_config()
        ):
            await self.app(scope, receive, send)
            return

        await self._reconcile_on_startup()

        user_id = ""
        for name, value in scope.get("headers", []):
            if name == b"x-user-id":
                user_id = value.decode("utf-8", "replace")
                break
        if not user_id:
            # 与框架 get_current_user_id 一致:缺失时框架会 401,中间件不拦
            await self.app(scope, receive, send)
            return

        # peek 请求体取 agent_id/session_id,回灌式 receive 交还下游
        cached_body, agent_id, session_id = await self._peek_body(receive)
        replay_receive = self._replay_receive(cached_body)

        try:
            await self._guard.reconcile(self._grace_secs)
            reason = await self._guard.try_acquire_ex(
                user_id, agent_id, base_dims=self._enabled,
            )
        except Exception:  # noqa: BLE001 — fail-open
            logger.exception("concurrency guard reconcile/acquire failed; fail-open")
            await self.app(scope, replay_receive, send)
            return

        if reason is None and session_id:
            # 占位成功 → 记"待转正"（登记在响应之后；两步之间 run 可能已结束）
            try:
                await self._guard.reserve(session_id)
            except Exception:  # noqa: BLE001 — fail-open:释放信号缺失只影响精度
                logger.exception(
                    "concurrency reserve failed for session %s",
                    session_id,
                )

        if reason is not None:
            headers = [(b"content-type", b"application/json")]
            body = _throttled_body(reason)
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return

        await self._pass_through_and_finalize(
            scope, replay_receive, send, user_id, agent_id,
        )

    @staticmethod
    async def _peek_body(receive: Receive) -> tuple[bytes, str, str]:
        """聚合完整请求体并提取 agent_id/session_id。

        返回 ``(缓存 body, agent_id, session_id)``;解析失败视为无(跳过
        agent 维度,登记/释放退化为按响应体里的 session_id)。
        """
        parts: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return b"", "", ""
        body = b"".join(parts)
        agent_id = ""
        session_id = ""
        if body:
            try:
                payload = json.loads(body)
                agent_id = str(payload.get("agent_id", "") or "")
                session_id = str(payload.get("session_id", "") or "")
            except Exception:  # noqa: BLE001 — 解析失败跳过 agent 维度
                agent_id = ""
                session_id = ""
        return body, agent_id, session_id

    @staticmethod
    def _replay_receive(cached_body: bytes) -> Receive:
        """回灌式 receive:先回放缓存 body,后续消息透传为断开。"""
        replayed = {"done": False}

        async def receive() -> Message:
            if not replayed["done"]:
                replayed["done"] = True
                return {"type": "http.request", "body": cached_body, "more_body": False}
            return {"type": "http.disconnect"}

        return receive

    async def _pass_through_and_finalize(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        user_id: str,
        agent_id: str,
    ) -> None:
        """转发下游,缓冲 JSON 响应体,结束时注册或回滚。"""
        status = 200
        content_type = ""
        body_parts: list[bytes] = []
        buffer = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, content_type, buffer
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = dict(message.get("headers", []))
                content_type = headers.get(b"content-type", b"").decode("latin-1", "replace")
                buffer = content_type.startswith("application/json")
                await send(message)
            elif message["type"] == "http.response.body":
                if buffer:
                    body_parts.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        await self._finalize(status, body_parts, user_id, agent_id)
                await send(message)
            else:
                await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:  # noqa: BLE001 — fail-open:回滚后上抛,由外层中间件统一处理
            logger.exception("concurrency guard downstream failed; rolling back")
            await self._rollback(session_id, user_id, agent_id)
            raise

    async def _finalize(
        self,
        status: int,
        body_parts: list[bytes],
        user_id: str,
        agent_id: str,
    ) -> None:
        body = b"".join(body_parts)
        session_id = ""
        try:
            payload = json.loads(body)
            session_id = str(payload.get("session_id", ""))
        except Exception:  # noqa: BLE001
            session_id = ""
        if status >= 200 and status < 300:
            if session_id:
                try:
                    await self._guard.register(session_id, user_id, agent_id)
                    return
                except Exception:  # noqa: BLE001 — 登记失败必须回滚,避免计数泄漏
                    logger.exception("concurrency guard register failed; rolling back")
            await self._rollback(session_id, user_id, agent_id)
        else:
            await self._rollback(session_id, user_id, agent_id)

    async def _rollback(
        self, session_id: str, user_id: str, agent_id: str = "",
    ) -> None:
        # 回滚不需要 session_id/agent_id 存在:HDEL/DECR 不存在的键无害
        try:
            await self._guard.rollback(session_id, user_id, agent_id)
        except Exception:  # noqa: BLE001 — fail-open
            logger.exception("concurrency guard rollback failed")
