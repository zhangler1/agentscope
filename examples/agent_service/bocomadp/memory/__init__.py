# -*- coding: utf-8 -*-
"""会话级记忆中间件能力包（bocomadp/memory）。

包含配置存取（store/config）、平台 HTTP 客户端（platform）、Redis 状态
原语（state）、记忆中间件（middleware）、提取执行（extractor）与静默
扫描器（sweeper）、配置 API（routers）。

装配入口：
- ``configure_memory_runtime``：main.py 注入运行时依赖（redis 客户端 /
  storage），供中间件计数触发与扫描器使用（本地 InMemory 模式为 None
  时仅检索注入可用，计数/提取降级关闭）；
- ``build_memory_middlewares``：agent 中间件链按需构造 MemoryMiddleware
  （memory_enabled 才装配）；
- ``install_memory``：create_app 后挂载 /api/memory/config 路由并包装
  lifespan 启动 MemorySweeper。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable, Sequence

from agentscope.middleware import MiddlewareBase

from bocomadp.memory import state as memory_state
from bocomadp.memory import store as memory_store
from bocomadp.memory.config import MemoryRuntimeConfig, get_memory_runtime_config
from bocomadp.memory.middleware import MemoryMiddleware
from bocomadp.memory.store import MemoryConfig

logger = logging.getLogger("as")

__all__ = [
    "MemoryConfig",
    "MemoryRuntimeConfig",
    "MemoryMiddleware",
    "configure_memory_runtime",
    "build_memory_middlewares",
    "list_agent_session_ids",
    "cleanup_agent_memory",
    "install_memory",
]

# ---------------------------------------------------------------------------
# 运行时依赖注入（main.py 装配点）
# ---------------------------------------------------------------------------

_redis_client_fn: Callable[[], Any] | None = None
_storage: Any = None


def configure_memory_runtime(
    *,
    redis_client_fn: Callable[[], Any] | None = None,
    storage: Any = None,
) -> None:
    """注入记忆功能运行时依赖。

    Args:
        redis_client_fn: 返回 async Redis client 的函数（如 main._get_redis_client）；
            None（InMemory 本地模式）→ 计数/心跳/提取关闭，仅检索注入可用。
        storage: 框架存储对象（取会话消息 / 会话列表用）。
    """
    global _redis_client_fn, _storage
    _redis_client_fn = redis_client_fn
    _storage = storage


def _runtime_redis() -> Any:
    """返回运行时 redis client（惰性）；无注入返回 None。"""
    if _redis_client_fn is None:
        return None
    try:
        return _redis_client_fn()
    except Exception:  # noqa: BLE001 — 连接未就绪时 fail-open
        logger.debug("memory: runtime redis client unavailable")
        return None


def _runtime_storage() -> Any:
    return _storage


# ---------------------------------------------------------------------------
# 中间件构建（agent 中间件链入口）
# ---------------------------------------------------------------------------


async def build_memory_middlewares(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[MiddlewareBase]:
    """读记忆配置；memory_enabled（且已注册 caller）才构造 MemoryMiddleware。

    检索注入走配置的 caller（未注册无从检索/入库，跳过并告警）；计数 /
    轮数触发依赖 redis（``configure_memory_runtime`` 注入），redis 缺失时
    中间件仍做检索注入但跳过计数/提取。
    """
    cfg = await _load_config(user_id, agent_id)
    if cfg is None:
        return []
    if not cfg.memory_enabled:
        return []
    if not cfg.caller:
        logger.warning(
            "memory: agent %s (user %s) enabled but unregistered "
            "(no caller) — skip memory middleware",
            agent_id,
            user_id,
        )
        return []

    redis = _runtime_redis()
    storage = _runtime_storage()
    rt_cfg = await get_memory_runtime_config()

    # 触发回调：抢锁后（middleware 内完成）同步执行提取；
    # 由装配点把 run_extract 的 recheck_active 关掉——轮数触发的会话
    # 心跳最新（刚完成一轮），就是要提取这一批。
    async def _trigger(turns: int) -> None:
        del turns
        if storage is None:
            logger.warning(
                "memory: extract skipped (no storage wired) session=%s",
                session_id,
            )
            return
        from bocomadp.memory.extractor import run_extract

        await run_extract(
            redis,
            storage,
            user_id,
            agent_id,
            session_id,
            cfg,
            rt_cfg,
            recheck_active=False,
        )

    middleware = MemoryMiddleware(
        user_id,
        agent_id,
        session_id,
        cfg,
        rt_cfg,
        redis=redis,
        trigger=_trigger if redis is not None else None,
    )
    return [middleware]


async def _load_config(user_id: str, agent_id: str) -> MemoryConfig | None:
    """读配置；读失败/无记录返回 None（不阻断中间件链）。"""
    try:
        from bocomadp.memory import store as memory_store

        return await memory_store.memory_get(user_id, agent_id)
    except Exception:  # noqa: BLE001 — 记忆功能不可用时纯透传
        logger.warning(
            "memory: config load failed for agent=%s user=%s",
            agent_id,
            user_id,
            exc_info=True,
        )
        return None


async def list_agent_session_ids(
    user_id: str,
    agent_id: str,
    *,
    storage: Any | None = None,
) -> list[str]:
    """返回某 agent 的全部会话 id（供删除/清扫其记忆状态使用）。

    - ``storage`` 缺省取 ``configure_memory_runtime`` 注入的运行时 storage；
    - 失败/无会话返回 ``[]``（best-effort，不抛错）；
    - 注意：需在 agent 删除**前**调用（删除后会话已不可枚举）。
    """
    if storage is None:
        storage = _runtime_storage()
    if storage is None:
        return []
    try:
        sessions = await storage.list_sessions(user_id, agent_id)
        return [s.id for s in sessions]
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning(
            "memory: list sessions failed user=%s agent=%s",
            user_id,
            agent_id,
            exc_info=True,
        )
        return []


async def cleanup_agent_memory(
    user_id: str,
    agent_id: str,
    *,
    session_ids: Sequence[str] = (),
    redis: Any | None = None,
) -> None:
    """删除某智能体的记忆残留（best-effort，不向调用方抛错）。

    删除 agent 后调用，清理两部分：
    - DB ``agent_memory_configs`` 行（``memory_store.memory_delete``）；
    - Redis 会话态：对 ``session_ids`` 逐个 ``zrem(active_sessions)`` /
      ``del(turns)`` / ``del(extract_lock)``。

    注意：会话 id 列表须在 **agent 删除前**由调用方快照传入（删除后
    sessions 已不可枚举）；``redis`` 缺省取 ``configure_memory_runtime``
    注入的运行时客户端，未注入（本地 InMemory 模式）则跳过 Redis 段。
    平台侧删除保持占位（仅本地清理）。
    """
    try:
        await memory_store.memory_delete(user_id, agent_id)
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning(
            "memory: cleanup config delete failed agent=%s user=%s",
            agent_id,
            user_id,
            exc_info=True,
        )

    if redis is None:
        redis = _runtime_redis()
    if redis is not None and session_ids:
        try:
            for sid in session_ids:
                await redis.zrem(memory_state.ACTIVE_ZSET, sid)
                await redis.delete(memory_state.turns_key(sid))
                await redis.delete(memory_state.lock_key(sid))
        except Exception:  # noqa: BLE001 — best-effort
            logger.warning(
                "memory: cleanup redis state failed agent=%s",
                agent_id,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# 路由 + 后台扫描器安装
# ---------------------------------------------------------------------------


def install_memory(app: Any) -> None:
    """create_app 后调用：挂载 /api/memory/config 路由 + 启动静默扫描器。

    - 路由：``routers.install(app)``（main 统一 /api 前缀）；
    - lifespan：包装现有 ``app.router.lifespan_context``，在框架资源就绪
      后启动 ``MemorySweeper``（仅当 redis + storage 均已注入）。
    """
    from bocomadp.memory import routers

    routers.install(app)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_memory_sweeper(inner_app: Any):
        async with original_lifespan(inner_app):
            storage = _runtime_storage()
            redis = _runtime_redis()
            task: asyncio.Task | None = None
            if storage is not None and redis is not None:
                from bocomadp.memory.sweeper import MemorySweeper

                sweeper = MemorySweeper(redis, storage)
                task = asyncio.create_task(sweeper.run_forever())
                logger.info("memory: silent sweep task started")
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()

    app.router.lifespan_context = _lifespan_with_memory_sweeper
    logger.info("memory: install_memory done (router + sweeper lifespan)")
