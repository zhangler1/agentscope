# -*- coding: utf-8 -*-
"""静默会话记忆扫描器：定时扫描 → 预检 → confirm_persisted_then_run。

候选来源 = ``list_memory_session_states(last_active_before=..., limit=...)``
；按 agent_id 读配置校验启用；静默且 ``turns >= 1`` 的会话经 ``bus.is_locked``
预检后以**真实 owner** 执行提取——``reply_id=None``。

超龄清理：``now - last_active_at >= state_ttl_secs`` 的会话按「遗忘」
处理——批量删会话运行时状态（每批一条语句，单轮批数封顶；下次对话从 1 重新
计数、重建游标；框架消息与平台已保存的记忆内容不动）。

一轮的顺序是**先清理、后提取**：否则超龄行仍会占着提取候选名额。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import config_store as memory_store
from .config import MemoryGlobalConfig
from .extractor import _lock_key, confirm_persisted_then_run
from .global_config_store import get_memory_global_config
from .storage._models import MemoryAgentConfigRecord
from .storage._port import MemoryStorage

logger = logging.getLogger("as")

#: 单批清理上限（条）。实测（20 万超龄行 / 26 万行表，MySQL 8 单表 InnoDB）：
#: 单批 1000 行的"挑 id + 点删" p50 ≈ 42ms、p95 ≈ 1s —— 锁窗口就是该事务的
#: 时长，故不宜再放大。
_PURGE_BATCH = 1000

#: 单轮清理最多批数（单轮上限 = ``_PURGE_BATCH × _PURGE_MAX_BATCHES`` = 2 万条）。
#: 日清理能力 ≈ 2 万 × 288 轮 = 576 万条/天，远大于现实新增速率（``state_ttl_days``
#: 下的稳态积压 ≈ 7 × 日均新增会话数）→ 无需做成运维旋钮；达上限打 warning
#: 供观测即可（要调就改这里发版）。
_PURGE_MAX_BATCHES = 20

#: 单轮提取候选上限（条）。提取单会话成本高（读消息窗口 + 平台调用），一轮
#: 500 条足够（``idle_minutes`` 默认 60 分钟下实际远小于此值）；配合
#: ``ORDER BY last_active_at``（最久静默优先）不会饥饿 —— 本轮没轮到的下轮
#: 仍是队首。
_EXTRACT_BATCH = 500


class MemorySweeper:
    """后台静默扫描器：``run_forever()`` 在独立 task 中持续运行。

    每轮重读全局配置（idle_minutes / sweep 间隔 / 保留窗口即时生效）；
    单会话提取失败不中断整轮扫描。
    """

    def __init__(
        self,
        message_bus: Any,
        memory_storage: MemoryStorage,
        storage: Any,
    ) -> None:
        self._message_bus = message_bus
        self._memory_storage = memory_storage
        self._storage = storage

    async def run_forever(self) -> None:
        """主循环：读配置 → 扫一轮 → 间隔等待；三层异常保护永不退出。"""
        while True:
            rt_cfg = await get_memory_global_config()
            try:
                await self._sweep_once(rt_cfg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("memory: sweep round failed")
            except BaseException:
                logger.exception("memory: sweep round fatal-guard, keep running")
            await asyncio.sleep(rt_cfg.sweep_interval_seconds)

    async def _sweep_once(self, rt_cfg: MemoryGlobalConfig) -> int:
        """扫描一轮：**先批量清理超龄、再补提取静默会话**；返回发起提取数。"""
        now = time.time()
        await self._purge_stale(rt_cfg, now)      # 自身容错，不阻断下一步
        return await self._extract_idle(rt_cfg, now)

    async def _purge_stale(
        self, rt_cfg: MemoryGlobalConfig, now: float
    ) -> int:
        """批量清理超龄会话状态（§2.5）：每批一条语句，单轮批数封顶。

        返回值 = 本轮处理掉的**候选条数**（SQL 为删除行数；Redis 含"Hash 已
        过期、只回收索引"的悬空成员），仅用于日志与积压告警。

        终止判据 ``n < _PURGE_BATCH``：取不满一批即说明没有下一批。批调用抛
        瞬态错误（1213 死锁 / 1205 锁等待超时 / 连接抖动）时本轮收手——前几
        批已提交、结果有效；**绝不让清理故障阻断同轮的提取**。
        """
        threshold = now - rt_cfg.state_ttl_secs    # 循环外算一次：同轮同基准
        total = 0
        for _ in range(_PURGE_MAX_BATCHES):
            try:
                n = await self._memory_storage.delete_stale_session_states(
                    threshold, _PURGE_BATCH
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "memory: purge batch failed (%s)；本轮已处理 %d 条，"
                    "下轮继续",
                    exc,
                    total,
                )
                break
            total += n
            if n < _PURGE_BATCH:                   # 取不满一批 = 没有下一批
                break
        if total:
            logger.info("memory: purged %d stale session states", total)
        if total >= _PURGE_BATCH * _PURGE_MAX_BATCHES:
            logger.warning(
                "memory: purge backlog persists（本轮已处理 %d 条，达单轮上限）",
                total,
            )
        return total

    async def _extract_idle(
        self, rt_cfg: MemoryGlobalConfig, now: float
    ) -> int:
        """静默会话补提取；返回发起提取的会话数。

        候选是「整个内存中间件写入的会话状态」（范围下推），**不限于当前
        进程**——多 worker 部署下任一实例扫描即可覆盖全部会话；锁预检 +
        提取锁天然去重（同一会话至多一个提取在跑）。
        """
        idle_seconds = rt_cfg.idle_minutes * 60
        attempts = 0

        enabled: dict[str, MemoryAgentConfigRecord] = {}
        try:
            for agent_id, cfg in await memory_store.list_extraction_enabled():
                enabled[agent_id] = cfg
        except Exception:
            logger.exception("memory: sweep list_enabled failed")
            return 0

        try:
            states = await self._memory_storage.list_memory_session_states(
                last_active_before=now - idle_seconds,
                limit=_EXTRACT_BATCH,
            )
        except Exception:
            logger.exception("memory: sweep session states read failed")
            return 0

        for state in states:
            cfg = enabled.get(state.agent_id)
            if cfg is None:
                continue  # 该 agent 未开启提取
            key = _lock_key(state.user_id, state.agent_id, state.session_id)
            try:
                if await self._message_bus.is_locked(key):
                    continue  # 已有提取在跑（轮数触发 / 其它扫描实例）
            except Exception:
                continue  # 预检异常按已锁处理（保守跳过，下轮再来）
            attempts += 1
            try:
                # 静默路径：消息必然已入库 → reply_id=None 跳过落库确认
                await confirm_persisted_then_run(
                    self._message_bus,
                    self._memory_storage,
                    self._storage,
                    state.user_id,
                    state.agent_id,
                    state.session_id,
                    cfg,
                    rt_cfg,
                    reply_id=None,
                )
            except Exception:
                logger.exception(
                    "memory: sweep extract failed for session=%s "
                    "(last_active=%s)",
                    state.session_id,
                    state.last_active_dt,
                )
        return attempts


__all__ = ["MemorySweeper"]
