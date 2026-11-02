# -*- coding: utf-8 -*-
"""记忆配置 API（``/memory/config[/{agent_id}]``） + 平台同步决策。

- per-agent（``PUT/GET/DELETE /{agent_id}``）：归属校验（agent 不属于
  该用户 → 404 不暴露存在性）+ 本地全量存储 + 平台注册
  best-effort：先落库，注册失败返回 ``register_status=false`` +
  ``register_error`` 而不 502，下次 PUT 自动补注册；
- 全局（``PUT/GET/DELETE ""``）：全局运行参数（登录即可，无归属校验）。

路由前缀为相对路径 ``/memory/config``：使用方自行决定挂载（例如经
``app.mount("/api", app)`` 后对外即 ``/api/memory/config[...]``，或直接
include 到根应用即 ``/memory/config[...]``）。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, ValidationError

from agentscope.app.deps import get_current_user_id

from . import config_store as memory_store
from . import global_config_store as gcs
from . import mapping
from . import platform as pf
from .config import MemoryGlobalConfig
from .storage._models import MemoryAgentConfigRecord

logger = logging.getLogger("as")

memory_router = APIRouter(prefix="/memory/config", tags=["memory"])


class _PatchBody(BaseModel):
    """PUT 请求体：任意已知/新增字段，extra=allow 动态透传到合并层。"""

    model_config = ConfigDict(extra="allow")


class MemoryAgentConfig(MemoryAgentConfigRecord):
    """对外 API 响应模型：Record 字段 + 平台注册状态。

    ``register_status`` / ``register_error`` 为瞬态响应字段（不入库）：

    - ``register_status`` = 当前是否已注册成功（即 ``bool(caller)``），
      PUT / GET 语义统一；
    - ``register_error`` 仅本次 PUT 注册失败时填充失败原因（``str(exc)``），
      成功、未触发注册、GET 时为 ``None``。
    """

    register_status: bool = False
    register_error: str | None = None


def _merge(
    existing: MemoryAgentConfigRecord | None,
    body: dict[str, Any],
    agent_id: str,
) -> MemoryAgentConfigRecord:
    """部分更新：传入字段覆盖，未传保持；无记录时用默认值起步。

    主键 agent_id 以路径参数为准；未知字段由 Record 的 extra=forbid
    拒绝（→ 422）。"""
    base = existing.model_dump() if existing is not None else {}
    base.update(body)
    base["agent_id"] = agent_id
    return MemoryAgentConfigRecord(**base)


async def _assert_agent_owner(user_id: str, agent_id: str) -> None:
    """归属校验：agent 不属于该用户（或不存在）→ 404。

    404 而非 403——不向无关用户暴露 agent 的存在性。归属真源是框架
    agent 表（公开方法 ``storage.get_agent``，_base.py）。
    """
    storage = memory_store._storage
    if storage is None:
        # 未装配（install_memory 前 / 纯本地脚本）：无归属依据，放行
        return
    agent = await storage.get_agent(user_id, agent_id)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent not found",
        )


# ---------------------------------------------------------------------------
# 平台占位接口（删除 / 提示词修改为占位，仅本地 + 日志 + TODO）
# ---------------------------------------------------------------------------


def _sync_delete_placeholder(agent_id: str, user_id: str) -> None:
    """平台删除接口占位：本地已删，平台删除留 TODO。"""
    logger.info(
        "memory: config deleted locally for agent=%s user=%s "
        "(platform delete TODO)",
        agent_id,
        user_id,
    )


def _sync_prompt_placeholder(agent_id: str, prompt: str) -> None:
    """平台提示词修改接口占位：本地已存，平台同步留 TODO。"""
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
    """写某智能体记忆配置：越权校验 + 先落库 + 注册 best-effort。

    - 本地先 ``upsert_memory_config``（平台故障时配置也不丢，不再 502）；
    - 双开关（注入/提取）任一开启且无 caller → 平台注册（请求经 mapping
      构造）：成功回写 caller（``register_status=true``），失败记日志
      （``register_status=false`` + ``register_error``，下次 PUT 自动补）；
    - ``caller`` 为注册回写值、服务端管控：body 传入一律忽略；
    - 双开关全关不注册（避免白注册）；
    - ``switch_epoch`` 服务端推导（仅 ``extraction_enabled`` 变化时 +1），
      客户端传入一律忽略（防篡改）；会话状态的惰性重置由 middleware /
      锁内兜底按 epoch 收敛，此处**不触碰任何会话状态**；
    - 已注册 agent 改提示词 → 占位同步（仅本地 + 日志）；
    - ``agentName`` 仅注册透传平台（不入库）。
    """
    await _assert_agent_owner(user_id, agent_id)
    data = body.model_dump(exclude_none=True)
    data.pop("agent_id", None)  # 主键以路径参数为准，不收 body 覆盖
    data.pop("switch_epoch", None)  # 服务端推导，客户端不可写
    data.pop("caller", None)  # 注册回写值，服务端管控，不收 body 覆盖
    agent_name = data.pop("agentName", None) or agent_id
    existing = await memory_store.get_memory_config(agent_id)
    try:
        cfg = _merge(existing, data, agent_id)
    except ValidationError as exc:  # 未知字段/非法类型 → 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    # switch_epoch 服务端推导（防篡改 + 惰性重置版本号）
    was_extraction = bool(existing and existing.extraction_enabled)
    base_epoch = existing.switch_epoch if existing is not None else 0
    cfg.switch_epoch = base_epoch + (
        1 if cfg.extraction_enabled != was_extraction else 0
    )
    # 先落库：平台注册失败时本地配置也已持久化
    try:
        await memory_store.upsert_memory_config(cfg)
    except Exception as exc:  # noqa: BLE001 — 存储失败
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="memory config write failed",
        ) from exc
    register_error: str | None = None
    # 平台同步决策：双开关任一开启且未注册才调平台注册
    if (cfg.injection_enabled or cfg.extraction_enabled) and not cfg.caller:
        try:
            caller = await pf.register_agent(
                mapping.build_register_request(cfg, agent_name)
            )
            cfg = await memory_store.upsert_memory_config(
                cfg.model_copy(update={"caller": caller})
            )
        except Exception as exc:  # noqa: BLE001 — 平台注册 best-effort
            register_error = str(exc)
            logger.warning(
                "memory: platform register failed for agent=%s: %s",
                agent_id,
                exc,
            )
    elif cfg.caller and "memory_prompt" in data:
        # 已注册 agent 修改提示词 → 占位同步（仅本地 + 日志，不真调平台）
        _sync_prompt_placeholder(agent_id, cfg.memory_prompt)
    return MemoryAgentConfig(
        **cfg.model_dump(),
        register_status=bool(cfg.caller),
        register_error=register_error,
    ).model_dump()


@memory_router.get("/{agent_id}", summary="读某智能体记忆配置")
async def get_agent_config(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """读某智能体记忆配置（越权校验）；无记录返回 404。"""
    await _assert_agent_owner(user_id, agent_id)
    cfg = await memory_store.get_memory_config(agent_id)
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory config not found",
        )
    return MemoryAgentConfig(
        **cfg.model_dump(), register_status=bool(cfg.caller)
    ).model_dump()


@memory_router.delete("/{agent_id}", status_code=204, summary="删除某智能体记忆配置")
async def delete_agent_config(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """删除某智能体记忆配置及其全部会话状态（越权校验 + 本地删 + 平台删除占位日志）。"""
    await _assert_agent_owner(user_id, agent_id)

    # 延迟导入：routers 在 memory/__init__ 装配期被 import，模块顶层导入
    # cleanup_agent_memory 会形成循环依赖。该函数内部「先删配置、再清状态」，
    # 状态清理失败只记日志（与删除 agent 联动语义一致）。
    from . import cleanup_agent_memory

    await cleanup_agent_memory(agent_id)
    _sync_delete_placeholder(agent_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# 全局运行参数端点（登录即可，无归属校验）
# ---------------------------------------------------------------------------


@memory_router.put("", summary="写全局记忆运行参数（partial payload）")
async def put_global_config(
    body: dict[str, Any],
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """写全局记忆运行参数：与现有值合并（不传保留、首次用默认）后校验落库。"""
    del user_id
    current = (await gcs.get_memory_global_config()).model_dump()
    merged = {**current, **body}
    try:
        rt = MemoryGlobalConfig(**merged)
    except ValidationError as exc:  # 非法字段/越界 → 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    try:
        await gcs.upsert_memory_global_config(rt)
    except Exception as exc:  # noqa: BLE001 — 存储失败
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="memory global config write failed",
        ) from exc
    logger.info("memory: global config set: %s", rt.model_dump())
    return rt.model_dump()


@memory_router.get("", summary="读全局记忆运行参数")
async def get_global_config(
    user_id: str = Depends(get_current_user_id),
) -> dict[str, Any]:
    """读全局记忆运行参数；无记录返回默认值。"""
    del user_id
    return (await gcs.get_memory_global_config()).model_dump()


@memory_router.delete("", status_code=204, summary="删除全局记忆运行参数（回退默认）")
async def delete_global_config(
    user_id: str = Depends(get_current_user_id),
) -> Response:
    """删除全局记忆运行参数；删除后运行参数回退代码默认值。"""
    del user_id
    await gcs.delete_memory_global_config()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def install(app: Any) -> None:
    """把 memory 配置路由挂到 app（使用方按需再加路径前缀）。"""
    app.include_router(memory_router)


__all__ = [
    "memory_router",
    "install",
    "MemoryAgentConfig",
    "put_agent_config",
    "get_agent_config",
    "delete_agent_config",
]
