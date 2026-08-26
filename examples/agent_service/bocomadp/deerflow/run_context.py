# -*- coding: utf-8 -*-
"""请求级 run 配置（请求体根路径 5 键）上下文与持久化模块。

run/stream 请求体**根路径**携带 ``mode / reasoning_effort /
thinking_enabled / is_plan_mode / subagent_enabled`` 五个字段。路由层在
spawn 后台 run 任务前经 ContextVar 注入（``asyncio.create_task`` 复制
当前上下文，值随之传播到 run 任务内）。

其中 ``thinking_enabled`` / ``reasoning_effort`` 由模型构建层
（``model_patch``）消费，写入模型 Parameters；其余键（``mode`` /
``is_plan_mode`` / ``subagent_enabled``）当前静默接受但不使用——
仅保留在 ``extract_run_context`` 的键集中，不产生任何控制行为。

持久化：与 custom_params 共用同一 Redis key（hash 字段 ``run_context``），
TTL 从 PG runtime_configs（``df_session_config_ttl``）读取、默认 4h；
后续请求（如 HITL 确认续跑）未携带时从 Redis 回退加载。Redis/PG 不可用
fail-open，不阻断 run 创建。原生 ``/chat/`` 路径不设置，ContextVar 保持
默认空 dict，下游行为与现状一致。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any

from ._session_store import (
    load_run_context as load_run_context_from_store,
    save_session,
)

logger = logging.getLogger(__name__)

#: 请求体根路径中允许提取的键（其余忽略）。
RUN_CONTEXT_KEYS: tuple[str, ...] = (
    "mode",
    "reasoning_effort",
    "thinking_enabled",
    "is_plan_mode",
    "subagent_enabled",
)

_run_context_ctx: ContextVar[dict[str, Any]] = ContextVar(
    "run_context",
    default={},
)


def extract_run_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """从请求体根路径中提取 5 个已知键，忽略未知键与值为 None 的键。"""
    if not isinstance(context, dict):
        return {}
    return {
        k: context[k]
        for k in RUN_CONTEXT_KEYS
        if k in context and context[k] is not None
    }


def set_run_context(params: dict[str, Any] | None) -> Token:
    """设置当前上下文的 run 配置，返回 reset 用的 token。"""
    return _run_context_ctx.set(params or {})


def reset_run_context(token: Token) -> None:
    """恢复 ContextVar 到 :func:`set_run_context` 之前的值。"""
    _run_context_ctx.reset(token)


def get_run_context() -> dict[str, Any]:
    """获取当前上下文的 run 配置（工具过滤层 / 模型构建层读取）。"""
    return _run_context_ctx.get()


async def save_run_context(session_id: str, params: dict[str, Any]) -> None:
    """保存会话级 run_context 到 Redis（与 custom_params 同 key，字段 run_context）。"""
    await save_session(session_id, run_context=params)


async def load_run_context(session_id: str) -> dict[str, Any] | None:
    """从 Redis 读取会话级 run_context（无记录/过期/Redis 不可用 → None）。"""
    return await load_run_context_from_store(session_id)


__all__ = [
    "RUN_CONTEXT_KEYS",
    "extract_run_context",
    "set_run_context",
    "reset_run_context",
    "get_run_context",
    "save_run_context",
    "load_run_context",
]
