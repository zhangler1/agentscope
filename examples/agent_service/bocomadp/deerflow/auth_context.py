# -*- coding: utf-8 -*-
"""认证上下文模块（对齐 deer-flow ``community/common/auth_context.py``）。

通过 ContextVar 在 deerflow 路由层与工具后端之间传递解析后的认证信息，
避免在每个 tool call 中重复解析 custom_params。解析入口为
:func:`resolve_auth_params`（对齐 deer-flow ``_resolve_auth_params``），
优先级：guwp-token > jrt-auth-code > okic-token > muwp-user > none。

ContextVar 随 ``asyncio.create_task`` 复制到后台 run 任务，工具中间件 /
后端在 run 任务内经 :func:`get_resolved_auth` 读取；路由层 spawn 后
reset 不影响已创建的子任务（同 ``custom_params`` 模块的传播语义）。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass
class ResolvedAuth:
    """解析后的认证信息。

    ``auth_mode`` 表示最终选用的认证方式，优先级：
    guwp-token > jrt-auth-code > okic-token > muwp-user > none。
    """

    auth_mode: Literal[
        "guwp-token",
        "jrt-auth-code",
        "okic-token",
        "muwp-user",
        "none",
    ]
    guwp_token: str = ""
    jrt_auth_code: str = ""
    okic_token: str = ""
    okic_type: str = ""
    muwp_user: dict[str, Any] = field(default_factory=dict)


_auth_ctx: ContextVar[ResolvedAuth] = ContextVar(
    "resolved_auth",
    default=ResolvedAuth(auth_mode="none"),
)


def set_resolved_auth(auth: ResolvedAuth) -> Token:
    """设置当前上下文的认证信息，返回 reset 用的 token。"""
    return _auth_ctx.set(auth)


def reset_resolved_auth(token: Token) -> None:
    """恢复 ContextVar 到 :func:`set_resolved_auth` 之前的值。"""
    _auth_ctx.reset(token)


def get_resolved_auth() -> ResolvedAuth:
    """获取当前上下文的认证信息（工具后端中调用）。"""
    return _auth_ctx.get()


def resolve_auth_params(
    custom_params: dict[str, Any] | None,
) -> ResolvedAuth:
    """从 custom_params 解析认证方案（对齐 deer-flow ``_resolve_auth_params``）。

    优先级：guwp-token > jrt-auth-code > okic-token > muwp-user > none。
    任一方案的凭据为空串 / 缺失则跳过，全部缺失时返回 ``none``。
    """
    if not custom_params:
        return ResolvedAuth(auth_mode="none")

    guwp_token = str(custom_params.get("guwp_token") or "")
    jrt_auth_code = str(custom_params.get("jrt_auth_code") or "")
    okic_token = str(custom_params.get("okic_token") or "")
    okic_type = str(custom_params.get("okic_type") or "")
    muwp_user = custom_params.get("muwp_user") or {}
    if not isinstance(muwp_user, dict):
        muwp_user = {}

    if guwp_token:
        return ResolvedAuth(auth_mode="guwp-token", guwp_token=guwp_token)
    if jrt_auth_code:
        return ResolvedAuth(
            auth_mode="jrt-auth-code",
            jrt_auth_code=jrt_auth_code,
        )
    if okic_token:
        return ResolvedAuth(
            auth_mode="okic-token",
            okic_token=okic_token,
            okic_type=okic_type,
        )
    if muwp_user:
        return ResolvedAuth(auth_mode="muwp-user", muwp_user=muwp_user)
    return ResolvedAuth(auth_mode="none")


from ._session_store import load_auth as _load_auth_from_store
from ._session_store import save_session


async def save_auth(session_id: str, auth: ResolvedAuth) -> None:
    """保存会话级鉴权快照到 Redis（与 custom_params 同 key 同 TTL）。"""
    await save_session(session_id, auth=auth)


async def load_auth(session_id: str) -> ResolvedAuth | None:
    """从 Redis 读取会话级鉴权快照（无记录/过期/Redis 不可用 → None）。"""
    return await _load_auth_from_store(session_id)


def build_auth_headers(headers: dict[str, str]) -> dict[str, str]:
    """按当前上下文的 ResolvedAuth 注入认证请求头（三工具共享）。

    对齐源项目三工具各自重复实现的同一逻辑，此处收敛为单点：
    guwp-token > jrt-auth-code > okic-token(+okic-type)。
    """
    auth = get_resolved_auth()
    if auth.auth_mode == "guwp-token" and auth.guwp_token:
        headers["guwp-token"] = auth.guwp_token
    elif auth.auth_mode == "jrt-auth-code" and auth.jrt_auth_code:
        headers["jrt-auth-code"] = auth.jrt_auth_code
    elif auth.auth_mode == "okic-token" and auth.okic_token:
        headers["okic-token"] = auth.okic_token
        headers["okic-type"] = auth.okic_type
    return headers


def attach_muwp_user(body: dict[str, Any]) -> dict[str, Any]:
    """auth_mode 为 muwp-user 且 muwp_user 非空时附加 REQ_BODY.muwpUser。

    返回传入的 ``body``（原地修改），便于链式调用。
    """
    auth = get_resolved_auth()
    if auth.auth_mode == "muwp-user" and auth.muwp_user:
        body.setdefault("REQ_BODY", {})["muwpUser"] = auth.muwp_user
    return body


__all__ = [
    "ResolvedAuth",
    "set_resolved_auth",
    "reset_resolved_auth",
    "get_resolved_auth",
    "resolve_auth_params",
    "save_auth",
    "load_auth",
    "build_auth_headers",
    "attach_muwp_user",
]
