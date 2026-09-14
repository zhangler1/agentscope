# -*- coding: utf-8 -*-
"""记忆提取执行器：增量窗口 → 尾部对齐 → 完整轮切片 → save_memories → 推进游标。

提取触发点（调用方先抢好提取锁）：
- 轮数触发（MemoryMiddleware 达 ``update_rounds`` 倍数，``recheck_active=False``）；
- 静默扫描（MemorySweeper 判定会话超时静默，``recheck_active=True`` 默认）。

窗口取真增量而非按条数近似：``memory:cursor:{member}`` 记录上一批实际发送
的最后一条消息坐标 ``(created_at, msg_id)``，本批只取游标之后的消息。游标在
Redis，多实例共享——提取可能由任一实例执行（轮数触发在回复所在实例，静默
扫描在扫描实例），不能依赖进程内存。

两处历史缺陷对应的兜底：

- **尾部悬空**：轮数触发发生在 ChatService 落库 assistant 消息（``_persist``）
  之前，刚完成的回复可能还没入库，窗口尾部会悬着一条没有回复的 user。这里在
  读窗口前先等 1 秒宽限（``_DANGLING_WAIT_SECS``）再读；届时仍缺失（回复
  失败落空 assistant 被过滤、实例崩溃等）则由完整轮切片截掉，该 user 留待
  下一批。
- **单边消息**：``_slice_complete_turns`` 保证发给平台的 payload 一定以 user
  开头、以 assistant 结尾，任何半轮（尾部悬空 user、头部孤立 assistant）都
  不发出。

run_extract 全权负责锁的生命周期：成功/失败/放弃都会释放提取锁；成功额外
推进游标并 ``clear_extract_state``（active_sessions 移除 + turns 计数清零，
游标保留为下一批起点）；失败则游标与 turns 都不动，下次重试窗口自动扩大。
"""
from __future__ import annotations

import asyncio
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
    get_cursor,
    lock_key,
    set_cursor,
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
#: 无游标（首次提取）时的兼容窗口：每轮按 user+assistant 计，取最近 2×W 条
_MESSAGES_PER_TURN = 2
#: 读窗口前的宽限等待（秒）：轮数触发紧跟回复之后，assistant 可能尚未落库
_DANGLING_WAIT_SECS = 1.0
#: 有游标时单批增量窗口上限（条）——防长会话一次取爆，token 预算另有截断
_MAX_WINDOW_MESSAGES = 200


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


def _msg_pos(msg: Msg) -> tuple[str, str]:
    """消息在 ``(created_at, msg_id)`` 总序下的坐标（与 messages 表排序键一致）。"""
    return (
        str(getattr(msg, "created_at", "") or ""),
        str(getattr(msg, "id", "") or ""),
    )


def _after_cursor(msg: Msg, cursor: tuple[str, str] | None) -> bool:
    """该消息是否严格晚于游标（``None`` 游标视为全部命中）。"""
    if cursor is None:
        return True
    return _msg_pos(msg) > cursor


async def _load_messages_after(
    storage: Any,
    user_id: str,
    session_id: str,
    cursor: tuple[str, str],
    want: int = _MAX_WINDOW_MESSAGES,
) -> list[Msg]:
    """取游标**之后**的消息（全局正序，最旧在前），最多 ``want`` 条。

    复用 ``list_messages`` 的 ``before`` 游标分页从最新往回翻，逐页按
    ``(created_at, msg_id)`` 总序过滤出严格晚于游标的条目；一旦某页出现
    不晚于游标的消息，说明已覆盖到上批末尾，停止翻页。
    """
    collected: list[Msg] = []
    before: str | None = None
    while True:
        page, has_more = await storage.list_messages(
            user_id,
            session_id,
            limit=_PAGE_SIZE,
            before=before,
        )
        if not page:
            break
        collected = [m for m in page if _after_cursor(m, cursor)] + collected
        if any(not _after_cursor(m, cursor) for m in page):
            break
        if not has_more:
            break
        before = page[0].id
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


def _valid_pairs(messages: list[Msg]) -> list[tuple[Msg, str, str]]:
    """过滤出有效对话消息：``(msg, role, text)``，role ∈ {user, assistant} 且有文本。

    空 assistant（回复失败落库的 ``content=[]``）与 tool/system 消息在此
    被剔除——它们不构成可提取的对话内容。
    """
    result: list[tuple[Msg, str, str]] = []
    for msg in messages:
        role = getattr(msg, "role", "")
        if role not in ("user", "assistant"):
            continue
        text = msg.get_text_content()
        if not text:
            continue
        result.append((msg, role, text))
    return result


def _slice_complete_turns(
    messages: list[Msg],
) -> tuple[list[dict[str, str]], Msg | None]:
    """把消息裁成完整轮并转成 payload。

    完整轮 = 以 user 开头、以 assistant 结尾的序列（允许一轮含多条 user，
    例如一次提交多条输入）。头部孤立 assistant（其 user 在游标之前）与尾部
    悬空 user（assistant 未落库/为空）都被截掉，保证不发单边消息。

    Args:
        messages (`list[Msg]`): 本批窗口消息（任意序，内部先做 role/文本过滤）。

    Returns:
        ``(payload, last_msg)``：payload 为 ``[{role, content}]``；``last_msg``
        为本批最后一条被发送的消息，用于推进游标。无可发送内容时返回
        ``([], None)``。
    """
    valid = _valid_pairs(messages)
    start = 0
    while start < len(valid) and valid[start][1] != "user":
        start += 1
    end = len(valid) - 1
    while end >= start and valid[end][1] != "assistant":
        end -= 1
    if start > end or end < 0 or start >= len(valid):
        return [], None
    kept = valid[start : end + 1]
    payload = [{"role": role, "content": text} for _, role, text in kept]
    return payload, kept[-1][0]


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

        # turns 仅作触发信号（达阈值才会调到这里）；窗口大小由游标决定
        turns_raw = await redis.get(turns_key(user_id, agent_id, session_id))
        turns = int(turns_raw) if turns_raw else 0
        if turns < 1:
            return False

        # 「轮数触发」（recheck_active=False）紧跟在一轮回复之后，assistant 消息
        # 可能还没落库（ChatService 在回复流结束后的 finally 才 upsert）→ 先给
        # 1 秒宽限再读窗口，避免只读到一条悬空的 user；「静默扫描」
        # （recheck_active=True）处理的是已静默 idle_minutes 的会话，消息必然
        # 已入库，无需等待。仍悬空（回复失败的空 assistant、实例崩溃等）由
        # _slice_complete_turns 截掉该 user，留待下一批。
        if not recheck_active:
            await asyncio.sleep(_DANGLING_WAIT_SECS)

        # 增量窗口：有游标 → 取游标之后的消息；首次（无游标）→ 2×turns 兼容窗口
        cursor = await get_cursor(redis, user_id, agent_id, session_id)
        want = (
            turns * _MESSAGES_PER_TURN
            if cursor is None
            else _MAX_WINDOW_MESSAGES
        )
        if cursor is None:
            messages = await _load_recent_messages(
                storage,
                user_id,
                session_id,
                want=want,
            )
        else:
            messages = await _load_messages_after(
                storage,
                user_id,
                session_id,
                cursor,
                want=want,
            )
        if not messages:
            return False

        messages = _truncate_to_budget(messages, rt_cfg.max_tokens)

        # 完整轮切片：payload 必以 user 开头、assistant 结尾，不发单边消息
        payload, last_msg = _slice_complete_turns(messages)
        if not payload or last_msg is None:
            logger.info(
                "memory: no complete turn to extract session=%s "
                "(turns=%d, window=%d)",
                session_id,
                turns,
                len(messages),
            )
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

        # 成功 → 推进游标 + 清理状态（turns 清零，游标保留为下一批起点）
        last_created, last_id = _msg_pos(last_msg)
        await set_cursor(
            redis,
            user_id,
            agent_id,
            session_id,
            last_created,
            last_id,
        )
        await clear_extract_state(redis, user_id, agent_id, session_id)
        release = True
        logger.info(
            "memory: extracted session=%s turns=%d messages=%d cursor=%s",
            session_id,
            turns,
            len(payload),
            last_id,
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
