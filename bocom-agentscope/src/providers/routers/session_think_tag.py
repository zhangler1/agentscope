# -*- coding: utf-8 -*-
"""会话级 think-tag 覆盖路由 —— 替代 deerflow custom_params 的请求级 add_think 通道。

本包面向 SDK 原生 ``chat()`` 接口（请求体无 custom_params），会话级
``<think>`` 注入覆盖通过以下端点管理（写入 Redis，TTL 4h 与会话
生命周期对齐，HITL 续跑等场景持续生效）：

- ``PUT    /ellm-models/session/{session_id}/think-tag``  设置覆盖
- ``DELETE /ellm-models/session/{session_id}/think-tag``  清除覆盖（回退模型表）
- ``GET    /ellm-models/session/{session_id}/think-tag``  查询当前覆盖

中间件（``providers.middleware.ellm_refresh``）每次模型调用前读取本覆盖，
优先级：会话级覆盖 > Redis 模型表（``bocomadp:model:think_tag``）> 默认
``False``。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

from providers.ellm_chat_model import (
    _SESSION_ADD_THINK_KEY_PREFIX,
    _get_async_redis,
)
from providers.middleware.ellm_refresh import _parse_add_think

logger = logging.getLogger(__name__)

session_think_tag_router = APIRouter(
    prefix="/ellm-models",
    tags=["ellm-models"],
)

#: 会话级覆盖 TTL（秒）：4h，与 bocomadp 会话级 custom_params 的
#: Redis 过期时长一致。
_SESSION_THINK_TAG_TTL_SECS = 4 * 3600


def _session_add_think_key(session_id: str) -> str:
    """会话级覆盖的 Redis key。"""
    return f"{_SESSION_ADD_THINK_KEY_PREFIX}{session_id}:add_think"


class SessionThinkTagRequest(BaseModel):
    """设置会话级 think-tag 覆盖的请求体。"""

    think_tag: bool = Field(
        description="会话级 <think> 注入覆盖：true=注入，false=不注入",
    )


class SessionThinkTagResponse(BaseModel):
    """会话级 think-tag 覆盖视图。"""

    session_id: str = Field(description="会话 id。")
    think_tag: bool | None = Field(
        description="当前生效的会话级覆盖；null=未设置（回退模型表）",
    )
    ttl_secs: int = Field(description="覆盖剩余有效期（秒），未设置时为 0。")


@session_think_tag_router.get(
    "/session/{session_id}/think-tag",
    response_model=SessionThinkTagResponse,
    summary="查询会话级 think-tag 覆盖",
)
async def get_session_think_tag(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
) -> SessionThinkTagResponse:
    """查询会话级覆盖；未设置返回 ``think_tag: null``。"""
    redis = await _get_async_redis()
    raw = await redis.get(_session_add_think_key(session_id))
    value = _parse_add_think(raw)
    if value is None:
        return SessionThinkTagResponse(
            session_id=session_id,
            think_tag=None,
            ttl_secs=0,
        )
    ttl = await redis.ttl(_session_add_think_key(session_id))
    return SessionThinkTagResponse(
        session_id=session_id,
        think_tag=value,
        ttl_secs=max(0, ttl),
    )


@session_think_tag_router.put(
    "/session/{session_id}/think-tag",
    response_model=SessionThinkTagResponse,
    summary="设置会话级 think-tag 覆盖",
)
async def put_session_think_tag(
    session_id: str,
    body: SessionThinkTagRequest,
    user_id: str = Depends(get_current_user_id),
) -> SessionThinkTagResponse:
    """写入会话级覆盖（``"1"`` / ``"0"``，EXPIRE 4h）。

    写入后同会话的后续模型调用优先采用该覆盖，高于 Redis 模型表。
    """
    key = _session_add_think_key(session_id)
    redis = await _get_async_redis()
    await redis.set(key, "1" if body.think_tag else "0")
    await redis.expire(key, _SESSION_THINK_TAG_TTL_SECS)
    logger.info(
        "session_think_tag: set session=%s think_tag=%s (user=%s)",
        session_id,
        body.think_tag,
        user_id,
    )
    return SessionThinkTagResponse(
        session_id=session_id,
        think_tag=body.think_tag,
        ttl_secs=_SESSION_THINK_TAG_TTL_SECS,
    )


@session_think_tag_router.delete(
    "/session/{session_id}/think-tag",
    response_model=SessionThinkTagResponse,
    summary="清除会话级 think-tag 覆盖",
)
async def delete_session_think_tag(
    session_id: str,
    user_id: str = Depends(get_current_user_id),
) -> SessionThinkTagResponse:
    """删除覆盖（幂等）；清除后回退 Redis 模型表驱动。"""
    key = _session_add_think_key(session_id)
    redis = await _get_async_redis()
    removed = await redis.delete(key)
    logger.info(
        "session_think_tag: deleted session=%s (removed=%s, user=%s)",
        session_id,
        bool(removed),
        user_id,
    )
    return SessionThinkTagResponse(
        session_id=session_id,
        think_tag=None,
        ttl_secs=0,
    )


__all__ = ["session_think_tag_router"]
