# -*- coding: utf-8 -*-
"""EUVD 记忆平台 SDK 门面：连接管理与三接口调用。

职责边界：请求构造唯一经 :mod:`memory.mapping`（单一真源），本层只管
SDK 客户端生命周期与响应判定——三方法均接收已构造好的请求对象。SDK 为
同步 requests 实现，调用统一 ``asyncio.to_thread`` 包装，避免阻塞事件循环。

异常约定（不引入自定义包装类）：全模块唯一对外异常类型为 SDK 原生的
:class:`MemoriesSdkError`（经本模块 re-export，调用方
``from memory.platform import MemoriesSdkError`` 捕获即可）：

- SDK 传输/HTTP 层异常（``MemoriesTimeoutError`` / ``MemoriesApiError``
  等子类）直接向上抛、不二次包装，``raw_response`` / ``status_code``
  等属性原样保留；
- ``resp.success`` 为假 → :class:`MemoriesSdkError`（错误信息在
  ``resp.error``，本模块构造的消息统一 ``memory`` 前缀）；
- 注册：``resp.result`` 取 ``caller``（驼峰转下划线后键名不变），空则
  :class:`MemoriesSdkError`；
- 检索：``resp.result`` 为记忆条目列表（无则空列表）；
- 录入：``resp.result == "success"``。
"""
from __future__ import annotations

import asyncio
from typing import Any

from euvd_memory_pysdk import MemoriesClient, MemoriesClientConfig
from euvd_memory_pysdk.exceptions import MemoriesSdkError
from euvd_memory_pysdk.models import (
    RegisterAgentRequest,
    SaveMemoriesStandardRequest,
    SearchMemoryRequest,
)

from .settings import MemorySettings, get_settings

_client: MemoriesClient | None = None


def _client_cfg(settings: MemorySettings | None = None) -> MemoriesClientConfig:
    """MemorySettings → MemoriesClientConfig。

    ``environment`` 显式传入并由 SDK 校验（合法值 sit/uat/uat2/prd，非法值
    直接 raise）；超时 / 连接池 / headers 显式 override；凭证等其余项交给
    ``from_env`` 的 ``EUVD_MEMORIES_*`` 环境变量链。构造失败（缺关键配置
    或 environment 非法）由 SDK 向上抛——fail-fast。
    """
    s = settings or get_settings()
    overrides: dict[str, Any] = {
        "connect_timeout": s.connect_timeout_seconds,
        "read_timeout": s.read_timeout_seconds,
        "pool_connections": s.pool_connections,
        "pool_maxsize": s.pool_maxsize,
    }
    if s.headers:
        overrides["headers"] = dict(s.headers)
    return MemoriesClientConfig.from_env(
        environment=(s.environment or None), **overrides
    )


def get_client() -> MemoriesClient:
    """返回已启动的 SDK 客户端；未启动抛 MemoriesSdkError（fail-fast）。"""
    if _client is None:
        raise MemoriesSdkError(
            "memory: SDK client 未启动 — install_memory lifespan 或 start_client()"
        )
    return _client


async def start_client(settings: MemorySettings | None = None) -> None:
    """启动 SDK 客户端（幂等：重复调用先关旧再建新）。"""
    global _client
    cfg = await asyncio.to_thread(_client_cfg, settings)
    new_client = await asyncio.to_thread(MemoriesClient, cfg)
    old = _client
    _client = new_client
    if old is not None:
        old.close()


async def close_client() -> None:
    """关闭 SDK 客户端（幂等）。"""
    global _client
    old, _client = _client, None
    if old is not None:
        old.close()


async def register_agent(request: RegisterAgentRequest) -> str:
    """注册智能体；成功返回 caller（存 Record.caller，只存不发）。"""
    resp = await asyncio.to_thread(get_client().register_agent, request)
    if not resp.success:
        raise MemoriesSdkError(
            f"memory register failed: {resp.error} tid={resp.tid}"
        )
    result = resp.result or {}
    if isinstance(result, list):  # 平台可能回 list 包装
        result = result[0] if result else {}
    caller = (result.get("caller") or "") if isinstance(result, dict) else ""
    if not caller:
        raise MemoriesSdkError(
            f"memory register response missing caller: {result!r} tid={resp.tid}"
        )
    return caller


async def search_memory(request: SearchMemoryRequest) -> list[dict]:
    """检索记忆；返回记忆条目列表（无则空列表）。"""
    resp = await asyncio.to_thread(get_client().search_memory, request)
    if not resp.success:
        raise MemoriesSdkError(
            f"memory search failed: {resp.error} tid={resp.tid}"
        )
    result = resp.result or []
    return result if isinstance(result, list) else []


async def save_memories(request: SaveMemoriesStandardRequest) -> None:
    """录入记忆；``resp.result == "success"`` 判定。"""
    resp = await asyncio.to_thread(get_client().save_memory_standard, request)
    if not resp.success:
        raise MemoriesSdkError(
            f"memory save failed: {resp.error} tid={resp.tid}"
        )
    if resp.result != "success":
        raise MemoriesSdkError(
            f"memory save result != success: {resp.result!r} tid={resp.tid}"
        )


__all__ = [
    "MemoriesSdkError",
    "get_client",
    "start_client",
    "close_client",
    "register_agent",
    "search_memory",
    "save_memories",
]
