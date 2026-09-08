# -*- coding: utf-8 -*-
"""Redis 状态原语（分布式记忆状态，snake_case 键名）。

- ``memory:active_sessions`` 有序集合：member=session_id，score=最后活跃时间戳；
  中间件心跳/回复后写入，扫描器据此找静默会话；
- ``memory:turns:{session_id}``：字符串计数（INCR），每正常完成一轮 +1；
- ``memory:extract_lock:{session_id}``：提取锁（SET NX EX），并发/扫描互斥。
"""
from __future__ import annotations

import time

ACTIVE_ZSET = "memory:active_sessions"
#: 提取锁默认 TTL（秒）——提取任务应远小于该时长，超时自动释放防死锁。
DEFAULT_LOCK_TTL_SECS = 600


def turns_key(session_id: str) -> str:
    return f"memory:turns:{session_id}"


def lock_key(session_id: str) -> str:
    return f"memory:extract_lock:{session_id}"


async def mark_active(redis, session_id: str, now: float | None = None) -> None:
    """心跳：把会话在 active_sessions 中的活跃时间刷新为 now（默认当前时间）。"""
    await redis.zadd(
        ACTIVE_ZSET,
        {session_id: now if now is not None else time.time()},
    )


async def incr_turn(redis, session_id: str) -> int:
    """正常完成一轮对话后计数 +1（Redis INCR：key 不存在从 1 起）。"""
    return int(await redis.incr(turns_key(session_id)))


async def try_acquire_lock(redis, session_id: str, ttl: int = DEFAULT_LOCK_TTL_SECS) -> bool:
    """尝试获取会话提取锁（SET NX EX）；成功返回 True，已被占用返回 False。"""
    ok = await redis.set(lock_key(session_id), "1", nx=True, ex=ttl)
    return bool(ok)


async def clear_extract_state(redis, session_id: str) -> None:
    """提取成功后清理：从 active_sessions 移除 + 删除轮数计数。"""
    await redis.zrem(ACTIVE_ZSET, session_id)
    await redis.delete(turns_key(session_id))


__all__ = [
    "ACTIVE_ZSET",
    "DEFAULT_LOCK_TTL_SECS",
    "turns_key",
    "lock_key",
    "mark_active",
    "incr_turn",
    "try_acquire_lock",
    "clear_extract_state",
]
