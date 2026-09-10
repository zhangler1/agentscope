# -*- coding: utf-8 -*-
"""记忆提取执行器：窗口=turns → 截断 → save_memories（重试 3 次）→ 清理状态。

提取触发点（调用方先抢好提取锁）：
- 轮数触发（MemoryMiddleware 达 ``update_rounds`` 倍数，``recheck_active=False``）；
- 静默扫描（MemorySweeper 判定会话超时静默，``recheck_active=True`` 默认）。

run_extract 全权负责锁的生命周期：成功/失败/放弃都会释放提取锁，
成功额外 ``clear_extract_state``（active_sessions 移除 + turns 计数清零）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from agentscope.message import Msg

from bocomadp.memory import platform as pf
from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.state import (
    ACTIVE_ZSET,
    clear_extract_state,
    encode_member,
    lock_key,
    turns_key,
)
from bocomadp.memory.store import MemoryConfig

logger = logging.getLogger("as")

#: token 粗估：字节数 / 4（中文场景近似 token 数）
_BYTES_PER_TOKEN = 4
#: list_messages 单页上限
_PAGE_SIZE = 50
#: 保存失败重试退避序列（秒）
_DEFAULT_RETRY_DELAYS = (1.0, 3.0, 9.0)
#: 每轮对话按 user+assistant 计，取最近 2×W 条消息近似 W 轮
_MESSAGES_PER_TURN = 2


async def _load_recent_messages(
    storage: Any,
    user_id: str,
    session_id: str,
    want: int,
) -> list[Msg]:
    """按游标分页取最近 ``want`` 条消息（返回全局正序：最旧在前）。"""
    collected: list[Msg] = []
    before: str | None = None
    while len(collected) < want:
        page, has_more = await storage.list_messages(
            user_id,
            session_id,
            limit=min(_PAGE_SIZE, want),
            before=before,
        )
        if not page:
            break
        collected = list(page) + collected
        if not has_more:
            break
        before = page[0].id
        if len(collected) >= want:
            break
    return collected[-want:] if collected else []


def _estimate_tokens(text: str) -> int:
    """粗略 token 估算：UTF-8 字节数 / 4。"""
    return max(1, len(text.encode("utf-8", errors="ignore")) // _BYTES_PER_TOKEN)


def _truncate_to_budget(messages: list[Msg], max_tokens: int) -> list[Msg]:
    """从最旧开始丢弃，直到剩余消息总量落在 max_tokens 预算内（保留最新）。"""
    kept = list(messages)
    while kept:
        total = sum(
            _estimate_tokens(m.get_text_content() or "")
            for m in kept
        )
        if total <= max_tokens:
            break
        kept.pop(0)
    return kept


def _to_role_content(messages: list[Msg]) -> list[dict[str, str]]:
    """过滤出 user/assistant 文本消息 → [{role, content}]（保留原始顺序）。"""
    result: list[dict[str, str]] = []
    for msg in messages:
        role = getattr(msg, "role", "")
        if role not in ("user", "assistant"):
            continue
        text = msg.get_text_content()
        if not text:
            continue
        result.append({"role": role, "content": text})
    return result


async def _is_session_active(
    redis: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
    idle_seconds: float,
    now: float | None = None,
) -> bool:
    """会话最后心跳距今 < idle_seconds 视为仍活跃。"""
    last = await redis.zscore(
        ACTIVE_ZSET,
        encode_member(user_id, agent_id, session_id),
    )
    if last is None:
        return False
    return (now if now is not None else time.time()) - float(last) < idle_seconds


async def run_extract(
    redis: Any,
    storage: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
    cfg: MemoryConfig,
    rt_cfg: MemoryRuntimeConfig,
    *,
    recheck_active: bool = True,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
) -> bool:
    """对会话执行一次记忆提取；返回是否成功保存。

    调用方必须先抢提取锁（``try_acquire_lock``）。本函数在退出前统一
    释放锁；成功路径额外清理状态（active_sessions / turns）。

    ``recheck_active``：拿锁后若会话重新活跃（心跳新于静默窗口）则放弃——
    默认 True（静默扫描场景）；轮数触发场景传 False（刚完成一轮、心跳最新，
    但就是要提取这一批）。
    """
    release = False
    try:
        if recheck_active and await _is_session_active(
            redis,
            user_id,
            agent_id,
            session_id,
            rt_cfg.idle_minutes * 60,
        ):
            logger.info(
                "memory: extract skip (session %s active again)", session_id,
            )
            return False

        # W = turns（INCR 计数值）；无计数视为无可提取内容
        turns_raw = await redis.get(turns_key(user_id, agent_id, session_id))
        turns = int(turns_raw) if turns_raw else 0
        if turns < 1:
            return False

        messages = await _load_recent_messages(
            storage,
            user_id,
            session_id,
            want=turns * _MESSAGES_PER_TURN,
        )
        if not messages:
            return False
        messages = _truncate_to_budget(messages, rt_cfg.max_tokens)
        payload = _to_role_content(messages)
        if not payload:
            return False

        param = {
            "agentId": agent_id,
            "userCode": user_id,
            "agentPlat": cfg.agent_plat,
            "memoryType": cfg.memory_type,
            "inferFlag": cfg.infer_flag,
            "messages": payload,
        }
        await _save_with_retry(param, retry_delays)

        # 成功 → 清理状态并释放锁（turns 清零，下一批从 1 重新计数）
        await clear_extract_state(redis, user_id, agent_id, session_id)
        release = True
        logger.info(
            "memory: extracted session=%s turns=%d messages=%d",
            session_id,
            turns,
            len(payload),
        )
        return True
    finally:
        # 统一释放提取锁（成功/失败/放弃都释放；失败保留 turns 由
        # 后续轮数/扫描再试，锁 TTL 兜底防并发）
        await redis.delete(lock_key(user_id, agent_id, session_id))


async def _save_with_retry(
    param: dict[str, Any],
    retry_delays: tuple[float, ...],
) -> None:
    """save_memories 失败重试（指数退避：默认 1s/3s/9s，共 3 次）。"""
    import asyncio

    attempt = 0
    while True:
        try:
            await pf.save_memories(param)
            return
        except pf.PlatformError:
            attempt += 1
            if attempt > len(retry_delays):
                raise
            delay = retry_delays[attempt - 1]
            if delay > 0:
                await asyncio.sleep(delay)
            logger.warning(
                "memory: save_memories failed (attempt %d/%d), retrying",
                attempt,
                len(retry_delays) + 1,
            )


__all__ = ["run_extract"]
