# -*- coding: utf-8 -*-
"""Redis 状态原语（分布式记忆状态，snake_case 键名）。

- ``memory:active_sessions`` 有序集合：member = ``encode_member(user_id, agent_id,
  session_id)``（复合标识，携带 owner 与 agent），score = 最后活跃时间戳；
- ``memory:turns:{member}``：字符串计数（INCR），每正常完成一轮 +1；
- ``memory:extract_lock:{member}``：提取锁（SET NX EX），并发/扫描互斥；
- ``memory:cursor:{member}``：提取游标 ``"{created_at}|{msg_id}"``——上一次
  提取实际发送的最后一条消息坐标（``(created_at, msg_id)`` 总序，与
  ``messages`` 表排序键一致）。多实例共享，用于取真增量而非按条数近似，
  成功提取后推进；失败保留（下次重试窗口扩大）。

复合标识使静默扫描器可从 ``active_sessions`` 直接反查每会话的 (user_id,
agent_id)，从而用**会话真实 owner** 执行提取（spec 5.1）。
"""
from __future__ import annotations

import time

#: 记忆模块所有 Redis key 的统一前缀。本模块是 memory 包**唯一**的 key
#: 来源，新增 key 一律经 :func:`_key` 拼接，从而保证全部以 ``memory:``
#: 开头、不与其他模块的数据抢命名空间。
KEY_PREFIX = "memory"


def _key(*parts: str) -> str:
    """拼一个带统一前缀的 key：``memory:<part>[:<part>...]``。"""
    return ":".join((KEY_PREFIX, *parts))


ACTIVE_ZSET = _key("active_sessions")
#: 提取锁默认 TTL（秒）——提取任务应远小于该时长，超时自动释放防死锁。
DEFAULT_LOCK_TTL_SECS = 600


def encode_member(user_id: str, agent_id: str, session_id: str) -> str:
    """复合标识：``user_id:agent_id:session_id``。"""
    return f"{user_id}:{agent_id}:{session_id}"


def parse_member(member: str) -> tuple[str, str, str]:
    """反解复合标识为 ``(user_id, agent_id, session_id)``。

    前提：三者均不含 ``:``（本项目 id 由框架生成，符合该约束）。
    输入不是三段式（如旧格式残留）时抛 ``ValueError``，调用方应 catch 跳过。
    """
    user_id, agent_id, session_id = member.split(":", 2)
    return user_id, agent_id, session_id


def turns_key(user_id: str, agent_id: str, session_id: str) -> str:
    return _key("turns", encode_member(user_id, agent_id, session_id))


def lock_key(user_id: str, agent_id: str, session_id: str) -> str:
    return _key("extract_lock", encode_member(user_id, agent_id, session_id))


def cursor_key(user_id: str, agent_id: str, session_id: str) -> str:
    return _key("cursor", encode_member(user_id, agent_id, session_id))


async def mark_active(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
    now: float | None = None,
) -> None:
    """心跳：把会话在 active_sessions 中的活跃时间刷新为 now（默认当前时间）。"""
    await redis.zadd(
        ACTIVE_ZSET,
        {
            encode_member(user_id, agent_id, session_id): (
                now if now is not None else time.time()
            )
        },
    )


async def incr_turn(redis, user_id: str, agent_id: str, session_id: str) -> int:
    """正常完成一轮对话后计数 +1（Redis INCR：key 不存在从 1 起）。

    计数**不设 TTL**：过期遗忘完全交给静默扫描器——按 ``state_ttl_days``
    修剪超龄会话（见 :func:`purge_session_state`）。
    """
    return int(await redis.incr(turns_key(user_id, agent_id, session_id)))


async def try_acquire_lock(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
    ttl: int = DEFAULT_LOCK_TTL_SECS,
) -> bool:
    """尝试获取会话提取锁（SET NX EX）；成功返回 True，已被占用返回 False。"""
    ok = await redis.set(
        lock_key(user_id, agent_id, session_id),
        "1",
        nx=True,
        ex=ttl,
    )
    return bool(ok)


async def get_cursor(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> tuple[str, str] | None:
    """读取提取游标 ``(created_at, msg_id)``；未提取过或值损坏时返回 ``None``。"""
    raw = await redis.get(cursor_key(user_id, agent_id, session_id))
    if not raw:
        return None
    created_at, sep, msg_id = str(raw).partition("|")
    if not sep or not created_at or not msg_id:
        return None
    return created_at, msg_id


async def set_cursor(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
    created_at: str,
    msg_id: str,
) -> None:
    """提取成功后把游标推进到本批最后一条已发送消息的坐标。

    游标**不设 TTL**：与轮数计数一样，过期遗忘交给静默扫描器的超龄修剪。
    """
    await redis.set(
        cursor_key(user_id, agent_id, session_id),
        f"{created_at}|{msg_id}",
    )


async def clear_extract_state(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> None:
    """提取成功后清理：从 active_sessions 移除 + 删除轮数计数。

    **不**删除 ``memory:cursor:{member}``——游标是下一批增量的起点；
    只有让每一批从上次结束处继续，才能避免按条数近似带来的重复/错位。
    """
    await redis.zrem(
        ACTIVE_ZSET,
        encode_member(user_id, agent_id, session_id),
    )
    await redis.delete(turns_key(user_id, agent_id, session_id))


async def purge_session_state(
    redis,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> None:
    """清空某会话的**全部**记忆运行时状态（超龄遗忘 / 彻底清理时用）。

    清除 active_sessions 成员 + turns 计数 + cursor 游标。与
    :func:`clear_extract_state` 的区别：后者是「本批提取完成」，保留游标
    作为下一批起点；本函数是「该会话作废」，游标一并清除，之后重新对话会
    从 1 重新计数、并重新建立游标。

    **不清** extract_lock：它自带 600s TTL，强行删除会破坏正在进行的提取
    的互斥；也不触碰 DB 消息与平台已保存的记忆内容。
    """
    await redis.zrem(
        ACTIVE_ZSET,
        encode_member(user_id, agent_id, session_id),
    )
    await redis.delete(
        turns_key(user_id, agent_id, session_id),
        cursor_key(user_id, agent_id, session_id),
    )


__all__ = [
    "ACTIVE_ZSET",
    "DEFAULT_LOCK_TTL_SECS",
    "KEY_PREFIX",
    "encode_member",
    "parse_member",
    "turns_key",
    "lock_key",
    "cursor_key",
    "mark_active",
    "incr_turn",
    "try_acquire_lock",
    "get_cursor",
    "set_cursor",
    "clear_extract_state",
    "purge_session_state",
]
