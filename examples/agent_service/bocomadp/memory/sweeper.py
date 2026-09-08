# -*- coding: utf-8 -*-
"""静默会话记忆扫描器：定时扫描 → 抢锁 → run_extract。

扫描维度：全部开启记忆的智能体（``store.memory_list_enabled``）→ 其会话
（``storage.list_sessions``）→ 在 active_sessions 中且最后心跳超过
``idle_minutes`` 的会话 → 轮数 turns>=1 → 抢提取锁 → ``run_extract``。

运行参数每次 tick 从 runtime_configs（key=``memory``）热读，热更新即时生效。
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
        """扫描一轮，返回本次提取尝试次数。"""
        idle_seconds = rt_cfg.idle_minutes * 60
        now = time.time()
        attempts = 0
        for user_id, agent_id, cfg in await memory_store.memory_list_enabled():
            for session_id in await self._list_session_ids_safe(user_id, agent_id):
                last = await self._redis.zscore(ACTIVE_ZSET, session_id)
                if last is None:
                    continue  # 不在活跃集：无记忆中间件心跳，忽略
                if (now - float(last)) < idle_seconds:
                    continue  # 静默窗口内仍活跃
                turns_raw = await self._redis.get(turns_key(session_id))
                turns = int(turns_raw) if turns_raw else 0
                if turns < 1:
                    continue  # 无可提取的完整轮次
                if not await try_acquire_lock(self._redis, session_id):
                    continue  # 已有提取在进行（轮数触发/其它扫描实例）
                attempts += 1
                try:
                    # run_extract 内部二次活跃确认 + 负责释放锁
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
                    await self._redis.delete(lock_key(session_id))
        return attempts

    async def _list_session_ids_safe(self, user_id: str, agent_id: str) -> list[str]:
        """取某 agent 的会话 id（失败返回空）——收敛到 memory 包统一实现。"""
        from bocomadp.memory import list_agent_session_ids

        return await list_agent_session_ids(
            user_id,
            agent_id,
            storage=self._storage,
        )


__all__ = ["MemorySweeper"]
