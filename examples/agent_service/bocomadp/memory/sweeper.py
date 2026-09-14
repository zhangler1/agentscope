# -*- coding: utf-8 -*-
"""静默会话记忆扫描器：定时扫描 → 抢锁 → run_extract。

扫描维度（spec 5.2，枚举反转）：从 ``active_sessions`` 活跃集出发，反查每个
会话的 (user_id, agent_id, session_id)；按 agent_id 读配置校验启用；静默且
turns>=1 的会话抢锁后以**真实 owner** 执行 run_extract——不再依赖配置归属者 user。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bocomadp.memory import store as memory_store
from bocomadp.memory.config import get_memory_runtime_config
from bocomadp.memory.extractor import run_extract
from bocomadp.memory.state import (
    ACTIVE_ZSET,
    lock_key,
    parse_member,
    try_acquire_lock,
    turns_key,
)

logger = logging.getLogger("as")


class MemorySweeper:
    """后台静默扫描器：``run_forever()`` 在独立 task 中持续运行。"""

    def __init__(self, redis: Any, storage: Any) -> None:
        self._redis = redis
        self._storage = storage

    async def run_forever(self) -> None:
        """循环执行扫描，间隔取 runtime_configs 的 sweep_interval_seconds。

        三层异常保护，保证后台任务不因单轮/意外异常而退出：
        - ``CancelledError``：服务关闭/事件循环退出时放行（不吞取消，
          否则 lifespan 的 task.cancel() 无法停止本任务）；
        - ``Exception``：常规单轮失败（redis/db 抖动、单会话异常），记日志
          后下一轮自动重试；
        - ``BaseException``（兜底）：防偶发的非 Exception 中断（如个别三方库
          抛的 BaseException 子类）把唯一后台任务打停并静默消失。
        """
        while True:
            rt_cfg = await get_memory_runtime_config()
            try:
                await self._sweep_once(rt_cfg)
            except asyncio.CancelledError:
                raise  # 正常关闭：放行取消
            except Exception:  # noqa: BLE001 — 单轮扫描失败不中断循环
                logger.exception("memory: sweep round failed")
            except BaseException:  # noqa: BLE001 — 防打穿兜底：记录后继续
                logger.exception(
                    "memory: sweep round fatal-guard, keep running",
                )
            await asyncio.sleep(rt_cfg.sweep_interval_seconds)

    async def _sweep_once(self, rt_cfg) -> int:
        """扫描一轮，返回本次提取尝试次数。

        候选来源是 ``active_sessions``（中间件心跳写入），而非
        ``storage.list_sessions(配置归属者, agent)``——共享 agent 下后者会漏掉
        非配置归属者的会话。
        """
        idle_seconds = rt_cfg.idle_minutes * 60
        now = time.time()
        attempts = 0

        # agent_id → 启用配置（供按 agent 校验；不携带归属者 user）
        enabled: dict[str, Any] = {}
        try:
            for agent_id, cfg in await memory_store.memory_list_enabled():
                enabled[agent_id] = cfg
        except Exception:  # noqa: BLE001 — 配置读失败本轮跳过
            logger.exception("memory: sweep list_enabled failed")
            return 0

        try:
            members = await self._redis.zrangebyscore(
                ACTIVE_ZSET,
                "-inf",
                "+inf",
                withscores=True,
            )
        except Exception:  # noqa: BLE001 — 活跃集读失败本轮跳过
            logger.exception("memory: sweep active_sessions read failed")
            return 0

        for member, last_score in members:
            try:
                user_id, agent_id, session_id = parse_member(member)
            except ValueError:
                continue  # 旧格式/异常成员，忽略
            cfg = enabled.get(agent_id)
            if cfg is None:
                continue  # 该 agent 未开启记忆
            if (now - float(last_score)) < idle_seconds:
                continue  # 静默窗口内仍活跃
            try:
                turns_raw = await self._redis.get(
                    turns_key(user_id, agent_id, session_id),
                )
            except Exception:  # noqa: BLE001
                continue
            turns = int(turns_raw) if turns_raw else 0
            if turns < 1:
                continue  # 无可提取的完整轮次
            if not await try_acquire_lock(
                self._redis,
                user_id,
                agent_id,
                session_id,
            ):
                continue  # 已有提取在进行（轮数触发/其它扫描实例）
            attempts += 1
            try:
                # 以会话真实 owner 提取：读消息 / userCode 均正确
                await run_extract(
                    self._redis,
                    self._storage,
                    user_id,
                    agent_id,
                    session_id,
                    cfg,
                    rt_cfg,
                )
            except Exception:  # noqa: BLE001 — 单会话失败不影响其它
                logger.exception(
                    "memory: sweep extract failed for session=%s",
                    session_id,
                )
                await self._redis.delete(lock_key(user_id, agent_id, session_id))
        return attempts


__all__ = ["MemorySweeper"]
