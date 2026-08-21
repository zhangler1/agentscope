# -*- coding: utf-8 -*-
"""会话级共享存储（custom_params + auth 同 key 同 TTL，纯 Redis）。

需求（设计文档 5.1，2026-08-20 用户改选 Redis）：把 custom_params 的
落盘持久化改为 Redis 存储；鉴权（ResolvedAuth）与 custom_params 存同一
Redis key 的不同 hash 字段、同一 TTL（4h）——两者同生共死。

存储形态：
- key:  ``bocomadp:session:{session_id}:custom_params``
- hash:  ``params``（custom_params JSON）/ ``auth``（ResolvedAuth JSON）
- TTL:   Redis 原生 ``EXPIRE`` 自动过期，**无需自建清扫任务**
- fail-open: Redis 不可用时 save 告警不抛、load 返回 None，不阻断 run
  （与 ``pool_config.py`` 语义一致）；生产消息总线已是 RedisMessageBus，
  同设施可用性一致

客户端：懒加载 ``redis.asyncio.Redis``（参数来自 AppConfig，连接超时 2s），
测试通过替换模块级 ``_redis`` 注入 fakeredis。多 worker / 多实例天然共享，
无进程内 dict 的 worker 隔离问题。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .auth_context import ResolvedAuth

logger = logging.getLogger(__name__)

#: 条目存活时长（秒）。用户约束：4 小时。
_TTL_SECONDS = 14400

#: Redis key 前缀。
_KEY_PREFIX = "bocomadp:session"

#: hash 字段名。
_FIELD_PARAMS = "params"
_FIELD_AUTH = "auth"

#: 懒加载 Redis 客户端；测试注入 fakeredis 实例。
_redis: Any = None


def _key(session_id: str) -> str:
    """构造 Redis key：bocomadp:session:{session_id}:custom_params。"""
    return f"{_KEY_PREFIX}:{session_id}:custom_params"


async def _get_redis() -> Any:
    """懒加载 Redis 客户端（连接参数来自 AppConfig；仿 pool_config）。"""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        from bocomadp.config.app_config import get_app_config

        redis_cfg = get_app_config().redis
        _redis = aioredis.Redis(
            host=redis_cfg.host,
            port=redis_cfg.port,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis


def _auth_to_dict(auth: "ResolvedAuth") -> dict[str, Any]:
    """ResolvedAuth → dict（dataclasses.asdict）。"""
    from dataclasses import asdict

    return asdict(auth)


def _auth_from_dict(data: dict[str, Any]) -> "ResolvedAuth":
    """dict → ResolvedAuth（缺失字段用默认值兜底）。"""
    from .auth_context import ResolvedAuth

    return ResolvedAuth(
        auth_mode=str(data.get("auth_mode") or "none"),
        guwp_token=str(data.get("guwp_token") or ""),
        jrt_auth_code=str(data.get("jrt_auth_code") or ""),
        okic_token=str(data.get("okic_token") or ""),
        okic_type=str(data.get("okic_type") or ""),
        muwp_user=data.get("muwp_user") or {},
    )


async def save_session(
    session_id: str,
    *,
    params: dict[str, Any] | None = None,
    auth: "ResolvedAuth | None" = None,
) -> None:
    """按字段 upsert：HSET 更新传入字段（保留另一字段）+ EXPIRE 刷新 TTL。

    fail-open：Redis 异常仅告警，不阻断 run 创建。
    """
    try:
        r = await _get_redis()
        key = _key(session_id)
        if params is not None:
            await r.hset(
                key,
                _FIELD_PARAMS,
                json.dumps(params, ensure_ascii=False),
            )
        if auth is not None:
            await r.hset(
                key,
                _FIELD_AUTH,
                json.dumps(_auth_to_dict(auth), ensure_ascii=False),
            )
        await r.expire(key, _TTL_SECONDS)
    except Exception:  # noqa: BLE001 —— 非致命，仅告警
        logger.warning(
            "session store: save failed for session %s (non-fatal)",
            session_id,
            exc_info=True,
        )


async def load_params(session_id: str) -> dict[str, Any] | None:
    """读取 custom_params；无记录/过期/Redis 不可用 → None。"""
    try:
        r = await _get_redis()
        raw = await r.hget(_key(session_id), _FIELD_PARAMS)
        if raw is None:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 —— 非致命，降级为 None
        logger.warning(
            "session store: load params failed for session %s (non-fatal)",
            session_id,
            exc_info=True,
        )
        return None


async def load_auth(session_id: str) -> "ResolvedAuth | None":
    """读取鉴权快照；无记录/过期/Redis 不可用 → None。"""
    try:
        r = await _get_redis()
        raw = await r.hget(_key(session_id), _FIELD_AUTH)
        if raw is None:
            return None
        return _auth_from_dict(json.loads(raw))
    except Exception:  # noqa: BLE001 —— 非致命，降级为 None
        logger.warning(
            "session store: load auth failed for session %s (non-fatal)",
            session_id,
            exc_info=True,
        )
        return None


__all__ = [
    "_FIELD_AUTH",
    "_FIELD_PARAMS",
    "_KEY_PREFIX",
    "_TTL_SECONDS",
    "load_auth",
    "load_params",
    "save_session",
]
