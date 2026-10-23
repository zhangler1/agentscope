# -*- coding: utf-8 -*-
"""memory 包入口：装配 API + 导出面

使用方三步接线::

    setup_memory(settings, storage, message_bus)   # 选后端+单例
    install_memory(app)                                        # 路由+lifespan
    # 智能体工厂（工厂签名与 extra_agent_middle_factories 一致，单能力直传）：
    # extra_agent_middlewares=build_memory_middlewares

装配形态：全局单例（``configure_settings`` + ``config_store`` 模块级
``_memory_storage``/``_storage``/``_message_bus`` 三引用，fail-fast 装配
校验）；平台 SDK client 与 sweeper task 由 ``install_memory`` 包装的
lifespan 启停（close 顺序：cancel sweeper → gather 后台提取 →
close_client → SQL 后端 close）。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from agentscope.app.storage._redis_storage import RedisStorage
from agentscope.app.storage._sql import AsyncSQLAlchemyStorage
from agentscope.middleware import MiddlewareBase

from . import config_store as memory_store
from . import platform as pf
from . import routers
from .config import GLOBAL_CONFIG_KEY, MemoryGlobalConfig
from .global_config_store import get_memory_global_config
from .middleware import MemoryMiddleware
from .platform import MemoriesSdkError
from .settings import (
    MemorySettings,
    configure_settings,
    get_settings,
    reset_settings,
)
from .storage import (
    MemoryAgentConfigRecord,
    MemorySessionStateRecord,
    MemoryStorage,
)
from .storage._redis_memory import RedisMemoryStorage
from .storage._sql_memory import SQLMemoryStorage
from .sweeper import MemorySweeper

logger = logging.getLogger("as")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def setup_memory(
    settings: MemorySettings,
    storage: Any,
    message_bus: Any,
    memory_storage: MemoryStorage | None = None,
) -> None:
    """按框架 storage 类型选记忆后端并装配全局单例。

    Args:
        settings: 部署配置（SDK 连接 + DB 连接；``configure_settings``
            全局单例，全必填形态由使用方构造）。
        storage: 框架 ``StorageBase``——后端选型 + 越权校验数据源。
        message_bus: 框架 ``MessageBus``（提取锁）。
        memory_storage: 显式记忆后端（测试 / 自定义后端直通，优先于
            isinstance 推断）。

    装配表：

    - ``RedisStorage`` → ``RedisMemoryStorage(storage.get_client(),
      state_ttl_secs=全局默认)``；client 未就绪（未 ``__aenter__``）→
      raise（fail-fast）；
    - ``AsyncSQLAlchemyStorage`` → ``SQLMemoryStorage(settings.engine
      共享 | settings.db_url 自建)``，两者皆空 → raise；
    - 其他类型且未显式传 → raise。

    同时记录框架 storage / bus（路由越权校验、中间件、sweeper 共用）。
    """
    configure_settings(settings)
    if memory_storage is None:
        if isinstance(storage, RedisStorage):
            client = storage.get_client()
            if client is None:
                raise RuntimeError(
                    "setup_memory: RedisStorage.get_client() "
                    "为 None —— 需在 storage __aenter__ 之后装配"
                )
            memory_storage = RedisMemoryStorage(
                client,
                state_ttl_secs=MemoryGlobalConfig().state_ttl_secs,
            )
        elif isinstance(storage, AsyncSQLAlchemyStorage):
            if settings.engine is not None:
                memory_storage = SQLMemoryStorage(engine=settings.engine)
            elif settings.db_url:
                memory_storage = SQLMemoryStorage(db_url=settings.db_url)
            else:
                raise ValueError(
                    "setup_memory: AsyncSQLAlchemyStorage "
                    "装配需 settings.db_url 或 settings.engine 之一"
                )
        else:
            raise TypeError(
                f"setup_memory: 不支持的框架 storage 类型 "
                f"{type(storage).__name__}（或显式传 memory_storage）"
            )
    memory_store._memory_storage = memory_storage
    memory_store._storage = storage
    memory_store._message_bus = message_bus
    logger.info(
        "memory: runtime configured backend=%s",
        type(memory_storage).__name__,
    )


def install_memory(
    app: Any,
    *,
    cleanup_on_agent_delete: bool = True,
) -> None:
    """挂 ``/memory/config`` 路由 + 包装 lifespan（SDK client / sweeper 启停）。

    启动：``start_client()``（SDK）→ 记忆后端 ``ensure_ready()``（幂等
    建表）→ sweeper task（``_memory_storage``/``_message_bus`` 就绪时；未装配仅
    warning，路由读侧宽松兜底）。关闭：cancel sweeper → gather 后台提取
    任务（``middleware._pending_extract``，return_exceptions=True）→
    ``close_client()`` → SQL 后端 ``close()``（共享 engine 不 dispose）。

    Args:
        app (`Any`):
            FastAPI 应用（``create_app(...)`` 的返回值）。
        cleanup_on_agent_delete (`bool`, defaults to `True`):
            是否挂 :class:`~memory.agent_memory_cleanup.AgentMemoryCleanupMiddleware`——在
            ``DELETE /agent/{agent_id}`` 成功后自动执行
            :func:`cleanup_agent_memory`。框架删除链路本身不调用记忆清理，
            默认开启可让服务模式“删 agent 即清记忆”。自定义了删除逻辑的
            使用方可以关掉，改为显式调用 ``cleanup_agent_memory``。

            与 ``app.add_middleware`` 的限制一致：必须在应用启动**之前**
            调用（常规用法 ``create_app`` 之后、``uvicorn.run`` 之前）。
    """
    routers.install(app)
    if cleanup_on_agent_delete:
        from .agent_memory_cleanup import AgentMemoryCleanupMiddleware

        app.add_middleware(AgentMemoryCleanupMiddleware)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_: Any):
        await pf.start_client()
        ms = memory_store._memory_storage
        sweeper_task: asyncio.Task | None = None
        if ms is not None:
            ensure = getattr(ms, "ensure_ready", None)
            if ensure is not None:
                await ensure()
        if ms is not None and memory_store._message_bus is not None:
            sweeper_task = asyncio.create_task(
                MemorySweeper(
                    memory_store._message_bus,
                    ms,
                    memory_store._storage,
                ).run_forever()
            )
        else:
            logger.warning(
                "memory: sweeper 未启动（setup_memory 未调用，"
                "路由将以宽松模式运行）"
            )
        try:
            async with inner(app_) as state:
                yield state
        finally:
            if sweeper_task is not None:
                sweeper_task.cancel()
            from .middleware import _pending_extract

            pending = list(_pending_extract)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if sweeper_task is not None:
                with suppress(asyncio.CancelledError):
                    await sweeper_task
            await pf.close_client()
            if isinstance(ms, SQLMemoryStorage):
                await ms.close()

    app.router.lifespan_context = lifespan


# ---------------------------------------------------------------------------
# 中间件工厂
# ---------------------------------------------------------------------------


async def build_memory_middlewares(
    user_id: str, agent_id: str, session_id: str
) -> list[MiddlewareBase]:
    """读配置（``get_memory_config``）→ 双开关任一开启才构造 MemoryMiddleware。

    依赖未装配（``setup_memory`` 未调用）→ ``[]``（fail-open：
    记忆缺席不阻塞对话）。caller 空时仍装配——检索注入依赖平台注册完成
    （``register_status=false`` 的窗口期：无 caller
    → 中间件纯透传计数，与现状一致）。
    """
    if memory_store._memory_storage is None or memory_store._message_bus is None:
        return []
    cfg = await memory_store.get_memory_config(agent_id)
    if cfg is None or not (
        cfg.injection_enabled or cfg.extraction_enabled
    ):
        return []
    global_cfg = await get_memory_global_config()
    return [
        MemoryMiddleware(
            user_id,
            agent_id,
            session_id,
            cfg,
            global_cfg,
            memory_storage=memory_store._memory_storage,
            storage=memory_store._storage,
            message_bus=memory_store._message_bus,
        )
    ]


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


async def cleanup_agent_memory(agent_id: str) -> None:
    """删 agent 记忆配置 + 清理其全部会话运行时状态（框架 agent 删除链路调用）。

    顺序是「**先删配置、后清状态**」：配置一删，``build_memory_middlewares``
    立即不再装配（``cfg is None`` → ``[]``），新写入随之停止（仅剩 in-flight
    请求的一个窗口），清理不会被新数据追着跑。

    清理走存储端口的一次调用；由 ``state_ttl_secs`` 自然过期与 sweeper 的超龄清理兜底——框架的 agent
    删除链路不应因此失败。未装配时仅返回（框架消息与平台已保存记忆不在本层职责内）。
    """
    await memory_store.delete_memory_config(agent_id)
    ms = memory_store._memory_storage
    if ms is None:
        return
    try:
        cleaned = await ms.delete_agent_session_states(agent_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "memory: cleanup session states of agent %s failed"
            "（配置已删；残留由 TTL 与超龄清理兜底）",
            agent_id,
        )
        return
    if cleaned:
        logger.info(
            "memory: cleaned %d session states of agent %s", cleaned, agent_id
        )


__all__ = [
    # 配置
    "MemorySettings",
    "configure_settings",
    "get_settings",
    "reset_settings",
    # 存储端口与实现
    "MemoryStorage",
    "SQLMemoryStorage",
    "RedisMemoryStorage",
    "MemoryAgentConfigRecord",
    "MemorySessionStateRecord",
    # 全局配置
    "MemoryGlobalConfig",
    "GLOBAL_CONFIG_KEY",
    # 平台
    "MemoriesSdkError",
    # 中间件与装配
    "MemoryMiddleware",
    "setup_memory",
    "install_memory",
    "build_memory_middlewares",
    "cleanup_agent_memory",
]
