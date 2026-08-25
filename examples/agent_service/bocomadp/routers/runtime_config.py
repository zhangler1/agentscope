# -*- coding: utf-8 -*-
"""运行时配置（RuntimeConfigs）通用管理路由。

按 ``key`` 对 ``runtime_configs`` 表做通用增删改查，不绑定任何具体配置段。
任何配置段（``summarization``、``personal_search`` 等）都用同一套接口，只是
``key`` 不同。

接口:
- ``GET    /config``          列出全部配置段
- ``GET    /config/{key}``    读某配置段
- ``PUT    /config/{key}``    UPSERT（增改一体）某配置段
- ``DELETE /config/{key}``    删除某配置段

存储：PostgreSQL ``runtime_configs`` 表（见 ``bocomadp/runtime_config_store.py``）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from agentscope.app.deps import get_current_user_id

from bocomadp.runtime_config_store import (
    config_delete,
    config_get,
    config_list,
    config_set,
)

logger = logging.getLogger(__name__)

runtime_config_router = APIRouter(
    prefix="/config",
    tags=["runtime-config"],
)


@runtime_config_router.get(
    "",
    summary="列出全部配置段",
)
async def list_all(
    user_id: str = Depends(get_current_user_id),
) -> dict[str, dict]:
    """列出 ``runtime_configs`` 表中全部配置段（key -> payload）。"""
    return await config_list()


@runtime_config_router.get(
    "/{key}",
    summary="读某配置段",
)
async def get_one(
    key: str,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """读 ``key`` 对应的配置；不存在返回 404。"""
    payload = await config_get(key)
    if payload is None:
        return JSONResponse(
            content={"detail": f"config {key!r} not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return payload


@runtime_config_router.put(
    "/{key}",
    summary="写某配置段（UPSERT：不存在则增，存在则改）",
)
async def put_one(
    key: str,
    payload: dict[str, Any],
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """UPSERT ``key`` 对应的配置段。"""
    await config_set(key, payload)
    logger.info("runtime_config: set %r = %r", key, payload)
    return {"key": key, "payload": payload}


@runtime_config_router.delete(
    "/{key}",
    summary="删除某配置段",
)
async def delete_one(
    key: str,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """删除 ``key`` 对应的配置段；不存在返回 404。"""
    deleted = await config_delete(key)
    if not deleted:
        return JSONResponse(
            content={"detail": f"config {key!r} not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return {"key": key, "deleted": True}


__all__ = ["runtime_config_router"]
