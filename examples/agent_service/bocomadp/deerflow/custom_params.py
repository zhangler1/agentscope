# -*- coding: utf-8 -*-
"""请求级自定义参数（custom_params）上下文与持久化模块。

对齐 deer-flow 新分支的 ``custom_params`` 机制：run/stream 请求体携带
``custom_params``（空间码、用户编码等），路由层在 spawn 后台 run 任务前
经 ContextVar 注入（``asyncio.create_task`` 复制当前上下文，值随之传播
到 run 任务内），工具侧中间件读取后强制覆盖模型传参，避免在每个
tool call 中重复解析请求。

持久化（2026-08-20 用户改选 Redis 存储）：请求携带 custom_params 时写入
会话级 Redis 存储（``bocomadp/deerflow/_session_store.py``，key
``bocomadp:session:{session_id}:custom_params``，hash 字段 ``params``，
TTL 4h 由 Redis 原生 ``EXPIRE`` 自动过期、无清扫任务）；后续请求
（如 HITL 确认续跑）未携带时从 Redis 回退加载，保证空间码约束
在会话生命周期内持续生效。Redis 不可用 fail-open（save 告警不抛、
load 返回 None），不阻断 run 创建。

原生 ``/chat/`` 路径不携带 custom_params，ContextVar 保持默认空 dict，
下游行为与现状一致。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any

from ._session_store import load_params, save_session

logger = logging.getLogger(__name__)

_custom_params_ctx: ContextVar[dict[str, Any]] = ContextVar(
    "custom_params",
    default={},
)


def set_custom_params(params: dict[str, Any] | None) -> Token:
    """设置当前上下文的自定义参数，返回 reset 用的 token。

    路由层在 spawn 后台任务前调用；spawn 完成后调用
    :func:`reset_custom_params` 恢复——``asyncio.create_task`` 已复制
    上下文快照，reset 不影响已创建的后台任务。
    """
    return _custom_params_ctx.set(params or {})


def reset_custom_params(token: Token) -> None:
    """恢复 ContextVar 到 :func:`set_custom_params` 之前的值。"""
    _custom_params_ctx.reset(token)


def get_custom_params() -> dict[str, Any]:
    """获取当前上下文的自定义参数（工具中间件中调用）。"""
    return _custom_params_ctx.get()


async def save_custom_params(session_id: str, params: dict[str, Any]) -> None:
    """保存会话级 custom_params 到 Redis（委托 _session_store，TTL 4h）。"""
    await save_session(session_id, params=params)


async def load_custom_params(session_id: str) -> dict[str, Any] | None:
    """从 Redis 读取会话级 custom_params（无记录/过期/Redis 不可用 → None）。"""
    return await load_params(session_id)


__all__ = [
    "set_custom_params",
    "reset_custom_params",
    "get_custom_params",
    "save_custom_params",
    "load_custom_params",
]
