# -*- coding: utf-8 -*-
"""工具超长输出 Redis 存储层(复刻 Claude Code <persisted-output> 策略)。

- 完整工具输出以 ``tool_result:{session_id}:{tool_call_id}`` 为键存入 Redis,
  带 TTL;TTL 与阈值等配置实时读取自 PG ``runtime_configs`` 表 ``tool_result`` 段。
- 聚合预算的替换决策冻结状态存 ``tool_result:replacement:{session_id}`` Hash:
  field = tool_call_id, value 约定为 —— 非空 = 替换文本(字节一致重放),
  空串 = 已见过但未替换(冻结, 永不替换)。
- 连接为模块级懒加载单例(模仿 runtime_config_store._get_engine),禁止绕过
  AppConfig 直接读环境变量。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis

from bocomadp.config import get_app_config
from bocomadp.config.app_config import ToolResultConfig
from bocomadp.runtime_config_store import get_typed_config

logger = logging.getLogger("as")

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

DEFAULT_TTL_SECONDS = 14400  # 4 小时

KEY_PREFIX = "tool_result"
REPLACEMENT_KEY_PREFIX = "tool_result:replacement"

# 模块级懒加载单例连接池(与 runtime_config_store._get_engine 同模式)
_redis: Any = None
_redis_lock = asyncio.Lock()


async def _get_redis() -> Any:
    """懒加载 Redis 连接池单例;连接参数取 AppConfig.redis(host/port)。"""
    global _redis
    if _redis is None:
        async with _redis_lock:
            if _redis is None:
                cfg = get_app_config().redis
                pool = aioredis.ConnectionPool(
                    host=cfg.host,
                    port=cfg.port,
                    decode_responses=True,
                )
                _redis = aioredis.Redis(connection_pool=pool)
    return _redis


async def get_tool_result_config() -> ToolResultConfig:
    """实时读取 ``tool_result`` 配置段;无记录 / 读失败 / 反序列化失败 → 默认值。

    DB 每次持久化前调用(与 SummarizationMiddleware 一致,无缓存)。
    """
    cfg = await get_typed_config("tool_result", ToolResultConfig)
    if cfg is None:
        return ToolResultConfig()
    return cfg


def build_key(session_id: str, tool_call_id: str) -> str:
    """构造完整内容键:``tool_result:{session_id}:{tool_call_id}``。"""
    return f"{KEY_PREFIX}:{session_id}:{tool_call_id}"


async def set_tool_result(
    session_id: str,
    tool_call_id: str,
    content: str,
) -> str:
    """完整内容写 Redis,TTL 实时读配置;返回键。

    Raises:
        Exception: Redis 不可用等——由调用方 catch 后降级透传。
    """
    cfg = await get_tool_result_config()
    key = build_key(session_id, tool_call_id)
    redis = await _get_redis()
    await redis.set(key, content, ex=cfg.ttl_seconds)
    return key


async def get_tool_result(
    session_id: str,
    tool_call_id: str,
) -> str | None:
    """读完整内容。键由当前会话构造——结构性归属校验,不存在返回 None。"""
    redis = await _get_redis()
    return await redis.get(build_key(session_id, tool_call_id))


async def set_replacement_state(
    session_id: str,
    mapping: dict[str, str],
) -> None:
    """批量写入聚合预算决策状态(原子 pipeline),并刷新整键 TTL。

    Args:
        mapping: tool_call_id -> 值;非空字符串 = 替换文本(重放),
            空串 = 已见未替换(冻结标记)。
    """
    cfg = await get_tool_result_config()
    redis = await _get_redis()
    hkey = f"{REPLACEMENT_KEY_PREFIX}:{session_id}"
    async with redis.pipeline(transaction=True) as pipe:
        for tool_call_id, value in mapping.items():
            await pipe.hset(hkey, tool_call_id, value)
        await pipe.expire(hkey, cfg.ttl_seconds)
        await pipe.execute()


async def get_replacement_state(session_id: str) -> dict[str, str]:
    """读全部决策状态;无状态返回空 dict。"""
    redis = await _get_redis()
    return await redis.hgetall(f"{REPLACEMENT_KEY_PREFIX}:{session_id}")


__all__ = [
    "PERSISTED_OUTPUT_TAG",
    "PERSISTED_OUTPUT_CLOSING_TAG",
    "DEFAULT_TTL_SECONDS",
    "get_tool_result_config",
    "build_key",
    "set_tool_result",
    "get_tool_result",
    "set_replacement_state",
    "get_replacement_state",
]
