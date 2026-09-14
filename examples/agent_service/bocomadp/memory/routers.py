# -*- coding: utf-8 -*-
"""记忆配置 API（/api/memory/config[/{agent_id}]） + 平台同步决策。

- per-agent（``PUT/GET/DELETE /{agent_id}``）：本地 payload 全量存储
  （``agent_memory_configs``，含默认值字段）+ 平台注册同步决策（无 caller
  才注册；已有 caller 时改提示词走占位日志）；
- 全局（``PUT/GET/DELETE ""``）：``runtime_configs`` 表 key=``memory``
  的运行参数（idle_minutes / sweep_interval_seconds / max_tokens）。

挂载后（main.py 统一 ``/api`` 前缀）对外路径为 ``/api/memory/config[...]``。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError

from agentscope.app.deps import get_current_user_id

from bocomadp.memory import platform as pf
from bocomadp.memory import store as memory_store
from bocomadp.memory.config import (
    MemoryRuntimeConfig,
    RUNTIME_CONFIG_KEY,
    get_memory_runtime_config,
)
from bocomadp.memory.store import MemoryConfig
from bocomadp.runtime_config_store import config_delete, config_set

logger = logging.getLogger("as")

memory_router = APIRouter(prefix="/memory/config", tags=["memory"])


class _PatchBody(BaseModel):
    """PUT 请求体：任意已知/新增字段，extra=allow 动态入库。"""

    model_config = ConfigDict(extra="allow")


def _merge(existing: MemoryConfig | None, body: dict[str, Any]) -> MemoryConfig:
    """部分更新：传入字段覆盖，未传保持；无记录时用默认值起步。"""
    base = existing.model_dump() if existing is not None else {}
    base.update(body)
    return MemoryConfig(**base)


# ---------------------------------------------------------------------------
# 平台占位接口（契约 §13.4/§13.5：删除 / 提示词修改为占位，仅本地 + 日志 + TODO）
# ---------------------------------------------------------------------------


def _sync_delete_placeholder(agent_id: str, user_id: str) -> None:
    """§13.4 平台删除接口占位：本地已删，平台删除留 TODO。"""
    logger.info(
        "memory: config deleted locally for agent=%s user=%s "
        "(platform delete TODO)",
        agent_id,
        user_id,
    )


def _sync_prompt_placeholder(agent_id: str, prompt: str) -> None:
    """§13.5 平台提示词修改接口占位：本地已存，平台同步留 TODO。"""
    logger.info(
        "memory: prompt updated locally for agent=%s len=%d "
        "(platform prompt sync TODO)",
        agent_id,
        len(prompt or ""),
    )


# ---------------------------------------------------------------------------
# per-agent 端点
# ---------------------------------------------------------------------------


@memory_router.put("/{agent_id}", summary="写某智能体记忆配置（partial payload）")
async def put_agent_config(
    agent_id: str,
    body: _PatchBody,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """写某智能体记忆配置：本地部分合并 + 全量落库 + 平台同步决策。

    - 记忆**已启用**（memory_enabled=true）且无 caller（未注册）→ 调平台
      注册，返回 caller 一并落库；memory_enabled=false 时不注册；
    - 已注册（caller 非空）且本次改提示词 → 占位同步（仅本地 + 日志）；
    - payload 全量存储（含默认值字段），库内可直接核对完整配置。
    """
    data = body.model_dump(exclude_none=True)
    # agentName 仅在注册时透传平台（不入库）；缺省回退 agent_id。
    agent_name = data.pop("agentName", None) or agent_id
    existing = await memory_store.memory_get(agent_id)
    try:
        cfg = _merge(existing, data)
    except ValidationError as exc:  # 非法字段类型 → 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    # 平台同步决策：仅记忆启用（memory_enabled=true）且未注册才调平台注册；
    # memory_enabled=false 时不做任何平台注册（避免白注册）。
    if cfg.memory_enabled and not cfg.caller:
        try:
            caller = await pf.register_agent({
                "agentId": agent_id,
                "agentName": agent_name,
                "agentPlat": cfg.agent_plat,
                "messageType": cfg.message_type,
                "memoryPrompt": cfg.memory_prompt or None,  # 空串省略
            })
        except Exception as exc:  # noqa: BLE001 — 平台同步失败
            logger.warning(
                "memory: platform register failed for agent=%s: %s",
                agent_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="memory platform sync failed",
            ) from exc
        cfg = cfg.model_copy(update={"caller": caller})
    elif cfg.caller and "memory_prompt" in data:
        # 已注册 agent 修改提示词 → 占位同步（仅本地 + 日志，不真调平台）
        _sync_prompt_placeholder(agent_id, cfg.memory_prompt)
    try:
        await memory_store.memory_upsert(user_id, agent_id, cfg)
    except Exception as exc:  # noqa: BLE001 — 存储失败
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="memory config write failed",
        ) from exc
    return cfg.model_dump()


@memory_router.get("/{agent_id}", summary="读某智能体记忆配置")
async def get_agent_config(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """读某智能体记忆配置；无记录返回 404。"""
    cfg = await memory_store.memory_get(agent_id)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory config not found",
        )
    return cfg.model_dump()


@memory_router.delete("/{agent_id}", status_code=204, summary="删除某智能体记忆配置")
async def delete_agent_config(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """删除某智能体记忆配置（本地删 + 平台删除占位日志）。"""
    await memory_store.memory_delete(agent_id)
    _sync_delete_placeholder(agent_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 全局运行参数端点（runtime_configs key=memory）
# ---------------------------------------------------------------------------


@memory_router.put("", summary="写全局记忆运行参数（partial payload）")
async def put_global_config(body: dict[str, Any]) -> dict[str, Any]:
    """写全局记忆运行参数：与现有值合并（不传保留、首次用默认）后校验落库。"""
    current = (await get_memory_runtime_config()).model_dump()
    merged = {**current, **body}
    try:
        rt = MemoryRuntimeConfig(**merged)
    except ValidationError as exc:  # 非法字段/越界 → 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    await config_set(RUNTIME_CONFIG_KEY, rt.model_dump())
    logger.info("memory: runtime config set: %s", rt.model_dump())
    return rt.model_dump()


@memory_router.get("", summary="读全局记忆运行参数")
async def get_global_config() -> dict[str, Any]:
    """读全局记忆运行参数；无记录返回默认值。"""
    return (await get_memory_runtime_config()).model_dump()


@memory_router.delete("", status_code=204, summary="删除全局记忆运行参数（回退默认）")
async def delete_global_config() -> Response:
    """删除全局记忆运行参数；删除后运行参数回退代码默认值。"""
    await config_delete(RUNTIME_CONFIG_KEY)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install(app: Any) -> None:
    """把 memory 配置路由挂到 app（main.py 再统一加 /api 前缀）。"""
    app.include_router(memory_router)


__all__ = [
    "memory_router",
    "install",
    "put_agent_config",
    "get_agent_config",
    "delete_agent_config",
]
