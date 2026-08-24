# -*- coding: utf-8 -*-
"""ELLM 模型管理路由 —— Redis 模型候选（``bocomadp:model:think_tag``）增删改查。

存储：Redis Hash ``bocomadp:model:think_tag``（与
``providers/ellm_chat_model.py`` 的 ``list_models`` 读取端共用同一 key）：

- Field = 模型名（如 ``deepseek-v4-flash``）
- Value = JSON（``{"think_tag": 1, "context_size": 1000000, \
"output_size": 384000}``）

- ``think_tag`` 开关：``1`` 启用 <think> 注入，``0`` 不启用
- ``context_size`` 上下文大小（token），默认 1000000
- ``output_size`` 最大输出 token 数，默认 384000

写入后 ``list_models`` 实时生效（每次查询重新读 Redis），无需重启服务。

接口：

- ``GET    /ellm-models``            列出全部模型
- ``GET    /ellm-models/{model}``    查询单个模型
- ``POST   /ellm-models``            新增模型（已存在 → 409）
- ``PUT    /ellm-models/{model}``    修改 think_tag / context_size / output_size / 模型名
- ``DELETE /ellm-models/{model}``    删除模型（不存在 → 404）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

from bocomadp.providers.ellm_chat_model import (
    _DEFAULT_CONTEXT_SIZE,
    _DEFAULT_OUTPUT_SIZE,
    _MODEL_THINK_TAG_KEY,
)

logger = logging.getLogger(__name__)

ellm_models_router = APIRouter(
    prefix="/ellm-models",
    tags=["ellm-models"],
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class EllmModelItem(BaseModel):
    """ELLM 模型条目（查询响应，think_tag 为布尔）。"""

    model: str = Field(description="模型名（Redis Hash 的 field）")
    think_tag: bool = Field(
        description="think_tag 开关（查询返回布尔）：true=启用 <think>，false=不启用",
    )
    context_size: int = Field(gt=0, description="上下文大小（token）")
    output_size: int = Field(gt=0, description="最大输出 token 数")


class EllmModelCreateRequest(BaseModel):
    """新增模型请求。"""

    model: str = Field(description="模型名（Redis Hash 的 field）")
    think_tag: int = Field(
        ge=0,
        le=1,
        description="think_tag 开关：1=启用 <think> 注入，0=不启用",
    )
    context_size: int = Field(
        default=_DEFAULT_CONTEXT_SIZE,
        gt=0,
        description="上下文大小（token），默认 1000000",
    )
    output_size: int = Field(
        default=_DEFAULT_OUTPUT_SIZE,
        gt=0,
        description="最大输出 token 数，默认 384000",
    )


class EllmModelUpdateRequest(BaseModel):
    """修改模型请求（支持改名 + 改 think_tag / context_size / output_size）。"""

    model: str | None = Field(
        default=None,
        description="新模型名（改名用；缺省保持原名）",
    )
    think_tag: int = Field(
        ge=0,
        le=1,
        description="think_tag 开关：1=启用 <think> 注入，0=不启用",
    )
    context_size: int | None = Field(
        default=None,
        gt=0,
        description="上下文大小（token）；缺省保持原值",
    )
    output_size: int | None = Field(
        default=None,
        gt=0,
        description="最大输出 token 数；缺省保持原值",
    )


# ---------------------------------------------------------------------------
# 存储层（Redis Hash bocomadp:model:think_tag）
# ---------------------------------------------------------------------------

_redis: Any = None
_redis_lock = asyncio.Lock()


async def _get_redis() -> Any:
    """懒加载 Redis 客户端（app config 连接参数）。"""
    global _redis
    if _redis is None:
        async with _redis_lock:
            if _redis is None:
                from bocomadp.config import get_app_config

                import redis.asyncio as aioredis

                cfg = get_app_config().redis
                _redis = aioredis.Redis(
                    host=cfg.host,
                    port=cfg.port,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
    return _redis


def _decode_meta(value: Any) -> tuple[bool, int, int]:
    """解析 Redis value 为 ``(think_tag, context_size, output_size)``。

    仅接受 JSON 格式：``{"think_tag": 1, "context_size": 1000000, \
"output_size": 384000}``；非 JSON 或字段缺失时回退默认值
    （think_tag=False，context_size / output_size 取默认值）。
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            think = data.get("think_tag") in (1, True)
            context_size = int(
                data.get("context_size") or _DEFAULT_CONTEXT_SIZE
            )
            output_size = int(
                data.get("output_size") or _DEFAULT_OUTPUT_SIZE
            )
            return think, context_size, output_size
    except (ValueError, TypeError):
        pass
    return False, _DEFAULT_CONTEXT_SIZE, _DEFAULT_OUTPUT_SIZE


def _encode_meta(
    think_tag: int,
    context_size: int,
    output_size: int,
) -> str:
    """int 字段 → Redis 存储 JSON 字符串。"""
    return json.dumps(
        {
            "think_tag": 1 if think_tag else 0,
            "context_size": int(context_size),
            "output_size": int(output_size),
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@ellm_models_router.get(
    "",
    response_model=list[EllmModelItem],
    summary="列出全部 ELLM 模型",
)
async def list_ellm_models(
    user_id: str = Depends(get_current_user_id),
) -> list[EllmModelItem]:
    """列出 Redis 中全部模型候选（field → think_tag / context_size / output_size）。"""
    redis = await _get_redis()
    mapping = await redis.hgetall(_MODEL_THINK_TAG_KEY)
    items = []
    for raw_name, raw_tag in (mapping or {}).items():
        name = (
            raw_name.decode("utf-8", "replace")
            if isinstance(raw_name, bytes)
            else str(raw_name)
        )
        think, context_size, output_size = _decode_meta(raw_tag)
        items.append(
            EllmModelItem(
                model=name,
                think_tag=think,
                context_size=context_size,
                output_size=output_size,
            ),
        )
    return items


@ellm_models_router.get(
    "/{model_name}",
    response_model=EllmModelItem,
    summary="查询单个 ELLM 模型",
)
async def get_ellm_model(
    model_name: str,
    user_id: str = Depends(get_current_user_id),
) -> EllmModelItem:
    """查询指定模型的 think_tag / context_size / output_size。"""
    redis = await _get_redis()
    tag = await redis.hget(_MODEL_THINK_TAG_KEY, model_name)
    if tag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found in Redis.",
        )
    think, context_size, output_size = _decode_meta(tag)
    return EllmModelItem(
        model=model_name,
        think_tag=think,
        context_size=context_size,
        output_size=output_size,
    )


@ellm_models_router.post(
    "",
    response_model=EllmModelItem,
    status_code=status.HTTP_201_CREATED,
    summary="新增 ELLM 模型",
)
async def create_ellm_model(
    body: EllmModelCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> EllmModelItem:
    """新增模型到 Redis；模型已存在 → 409。"""
    redis = await _get_redis()
    exists = await redis.hexists(_MODEL_THINK_TAG_KEY, body.model)
    if exists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model {body.model!r} already exists.",
        )
    await redis.hset(
        _MODEL_THINK_TAG_KEY,
        body.model,
        _encode_meta(body.think_tag, body.context_size, body.output_size),
    )
    logger.info(
        "ellm_models: created model=%s think_tag=%d context_size=%d "
        "output_size=%d (user=%s)",
        body.model,
        body.think_tag,
        body.context_size,
        body.output_size,
        user_id,
    )
    return EllmModelItem(
        model=body.model,
        think_tag=bool(body.think_tag),
        context_size=body.context_size,
        output_size=body.output_size,
    )


@ellm_models_router.put(
    "/{model_name}",
    response_model=EllmModelItem,
    summary="修改 ELLM 模型（think_tag / 模型名）",
)
async def update_ellm_model(
    model_name: str,
    body: EllmModelUpdateRequest,
    user_id: str = Depends(get_current_user_id),
) -> EllmModelItem:
    """修改模型的 think_tag / context_size / output_size，可选改名；不存在 → 404。

    改名时删除旧 field、写入新 field（原子性由 Redis 单命令保证不了，
    先写新 key 再删旧 key；若两者相同则仅更新值）。
    """
    redis = await _get_redis()
    target = body.model or model_name
    # 读取旧值：context_size / output_size 缺省时保持原值
    old = await redis.hget(_MODEL_THINK_TAG_KEY, model_name)
    if old is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found in Redis.",
        )
    _, old_context_size, old_output_size = _decode_meta(old)
    context_size = (
        body.context_size
        if body.context_size is not None
        else old_context_size
    )
    output_size = (
        body.output_size if body.output_size is not None else old_output_size
    )
    new_meta = _encode_meta(body.think_tag, context_size, output_size)
    if target != model_name:
        # 新名已存在且不是自己 → 冲突
        conflict = await redis.hexists(_MODEL_THINK_TAG_KEY, target)
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Model {target!r} already exists.",
            )
        await redis.hset(_MODEL_THINK_TAG_KEY, target, new_meta)
        await redis.hdel(_MODEL_THINK_TAG_KEY, model_name)
    else:
        await redis.hset(_MODEL_THINK_TAG_KEY, model_name, new_meta)
    logger.info(
        "ellm_models: updated model=%s->%s think_tag=%d context_size=%d "
        "output_size=%d (user=%s)",
        model_name,
        target,
        body.think_tag,
        context_size,
        output_size,
        user_id,
    )
    return EllmModelItem(
        model=target,
        think_tag=bool(body.think_tag),
        context_size=context_size,
        output_size=output_size,
    )


@ellm_models_router.delete(
    "/{model_name}",
    summary="删除 ELLM 模型",
)
async def delete_ellm_model(
    model_name: str,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """删除模型；不存在 → 404。"""
    redis = await _get_redis()
    removed = await redis.hdel(_MODEL_THINK_TAG_KEY, model_name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found in Redis.",
        )
    logger.info(
        "ellm_models: deleted model=%s (user=%s)",
        model_name,
        user_id,
    )
    return {"deleted": True, "model": model_name}


__all__ = ["ellm_models_router"]
