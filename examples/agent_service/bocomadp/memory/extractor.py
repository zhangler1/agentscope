# -*- coding: utf-8 -*-
"""记忆提取执行器：把会话中的完整轮次推给平台落库，并推进提取游标。

入口 :func:`confirm_persisted_then_run`（后台任务体）：

- 轮数触发——middleware 达 ``update_rounds`` 后 ``create_task``，携带
  ``reply_id``（本轮回复的消息 id）；
- 静默扫描——sweeper 对已静默 ``idle_minutes`` 的会话直接调用，
  ``reply_id=None``（消息必然已入库，跳过确认）。

**锁外落库确认**：轮数触发紧跟回复之后，assistant 消息可能尚未
落库。以「读最新一条」取代轮询：``sleep(dangling_wait_secs)`` → 读会话最新
消息，其 id == ``reply_id`` 即确认；未中则退避 ×2、×4 再读（共 3 次读、总
等待 ≤7s）。穷尽后无论是否确认都继续——完整轮切片截掉悬空尾部兜底。

**锁内执行序列**（``message_bus.acquire_lock`` 排队串行化，共 5 步）：

1. ``get_memory_session_state`` → ``None`` 退出（sweeper 已清理）；
2. 读窗口：游标之后（首次无游标取最近），条数 ``_MAX_WINDOW_MESSAGES``
   与 token 预算双层上限，截断 log warning；空窗口退出（游标去重——
   排队接续者的空转出口）；
3. 完整轮切片：payload 必以 user 开头、assistant 结尾；``last_msg_id``
   = 本批末条被发送 assistant 消息 id（落库幂等锚点）；
4. ``build_save_request`` + ``save_memories``（重试退避 (1.0, 3.0) 共 3 击）；
5. 先平台后游标：``set_memory_session_cursor`` → ``reset_memory_session_turns``
   （游标推进后置 turns 失败仅多触发、不丢不重，log warning）。

「未含本轮回复 → turns 置 1」：确认穷尽（reply_id 未落库）且本批末条
assistant id != reply_id 时，游标照常推进到本批末条（只结清已落库的
内容），turns 置 1 而非清零——保证静默扫描（``turns >= 1``）与下一轮
累计仍会把尾部那一轮补齐。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agentscope.message import Msg

from . import platform as pf
from .config import MemoryGlobalConfig
from .mapping import build_save_request
from .settings import get_settings
from .storage._models import MemoryAgentConfigRecord
from .storage._port import MemoryStorage

logger = logging.getLogger("as")

#: token 粗估：UTF-8 字节数 / 2（汉字 3 字节 → ≈1.5 token/字，偏保守）
_BYTES_PER_TOKEN = 2
#: list_messages 单页上限
_PAGE_SIZE = 50
#: 保存失败重试退避序列（秒）——1 初次 + 2 重试共 3 击
_DEFAULT_RETRY_DELAYS = (1.0, 3.0)
#: 单批增量窗口上限（条）——防长会话一次取爆，token 预算另有截断
_MAX_WINDOW_MESSAGES = 50
#: 落库确认的窗口读次数（退避 ×2、×4；默认 1s/2s/4s 总等待 ≤7s）
_CONFIRM_ATTEMPTS = 3

_LOCK_KEY = "agentscope:memory:extract_lock:{user_id}:{agent_id}:{session_id}"


def _lock_key(user_id: str, agent_id: str, session_id: str) -> str:
    """锁键拼接（本模块加锁与 middleware 预检跨模块共用）。"""
    return _LOCK_KEY.format(
        user_id=user_id, agent_id=agent_id, session_id=session_id
    )


# ---------------------------------------------------------------------------
# 纯函数：分页读窗 / 坐标 / 过滤 / 预算 / 轮次切片（签名不变，测试复用）
# ---------------------------------------------------------------------------


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
    """粗略 token 估算：UTF-8 字节数 / 2。"""
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

    完整轮 = 以 user 开头、以 assistant 结尾的序列（允许一轮含多条 user）。
    头部孤立 assistant 与尾部悬空 user 都被截掉，保证不发单边消息。

    Returns:
        ``(payload, last_msg)``：payload 为 ``[{role, content}]``；
        ``last_msg`` 为本批最后一条被发送的消息（必为 assistant），用于
        落库 last_msg_id 与游标推进。无可发送内容时返回 ``([], None)``。
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


# ---------------------------------------------------------------------------
# 落库确认（锁外）与锁内执行序列
# ---------------------------------------------------------------------------


async def _read_window(
    storage: Any,
    user_id: str,
    session_id: str,
    cursor: tuple[str, str] | None,
) -> list[Msg]:
    """读窗口：有游标取增量，无游标（首次）取最近一批。"""
    if cursor is None:
        return await _load_recent_messages(
            storage,
            user_id,
            session_id,
            want=_MAX_WINDOW_MESSAGES,
        )
    return await _load_messages_after(
        storage, user_id, session_id, cursor
    )


def _cursor_of(state: Any) -> tuple[str, str] | None:
    """从会话状态记录取游标坐标（空游标 → None）。"""
    if state is None or not state.cursor_msg_id:
        return None
    return (state.cursor_created_at, state.cursor_msg_id)


class _GiveUp:
    """游标定位放弃哨兵（窗口为空 / 异常，无法确定起点）。"""

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return "GIVE_UP"


GIVE_UP = _GiveUp()


async def locate_cursor_before_message(
    storage: Any,
    user_id: str,
    session_id: str,
    user_msg_id: str | None,
    *,
    want: int = 20,
) -> tuple[str, str] | None:
    """定位“锚点 user 消息的前一条”游标坐标。

    三态返回：

    - ``(created_at, msg_id)``：正常游标（锚点前一条）——提取窗口从
      锚点 user 开始；
    - ``None``：锚点是会话首条 → 空游标（首次提取取最近 50，均为
      开启期内容，不违背铁律）；
    - :data:`GIVE_UP`：窗口为空 / 读异常 / 窗口内无 user 消息 → 放弃
      本次建档或重置（调用方本轮不计不建，下轮重试——安全侧：宁可
      不提取，绝不回捞）。

    锚点不在窗口内（落库异常等）时退化为保守规则：游标 = 窗口内
    **最后一条 role=user 本身**——旧 user 全部落在游标之前或等于游标
    （等于那条之后的孤儿 assistant 会被 ``_slice_complete_turns``
    丢弃），且后续落库的新消息在游标之后，不会被跳过。
    """
    try:
        msgs = await _load_recent_messages(
            storage, user_id, session_id, want=want
        )
    except Exception:
        logger.exception(
            "memory: locate cursor read failed session=%s", session_id
        )
        return GIVE_UP
    if not msgs:
        return GIVE_UP
    anchor = None
    if user_msg_id:
        for i, msg in enumerate(msgs):
            if str(msg.id) == str(user_msg_id):
                anchor = i
                break
    if anchor is not None:
        if anchor == 0:
            return None  # 锚点是会话首条 → 空游标
        return _msg_pos(msgs[anchor - 1])
    # 兜底：锚点不可见 → 游标 = 窗口内最后一条 role=user 本身
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == "user":
            return _msg_pos(msgs[i])
    return GIVE_UP


async def _confirm_reply_persisted(
    storage: Any,
    user_id: str,
    session_id: str,
    reply_id: str,
    wait_secs: float,
) -> bool:
    """锁外落库确认：会话最新一条消息 id == reply_id 即确认。

    只读最新一条即可——本轮 user 先落库，故"游标之后"必然非空，两种
    读法（按游标读增量 / 读最新一条）的末条都是全局最新一条。退避 ×2、
    ×4（共 3 次读、总等待 ≤7s）；穷尽返回 False（调用方继续，完整轮
    切片兜底）。
    """
    delay = wait_secs
    for _ in range(_CONFIRM_ATTEMPTS):
        await asyncio.sleep(delay)
        latest = await _load_recent_messages(
            storage, user_id, session_id, want=1
        )
        if latest and str(latest[-1].id) == reply_id:
            return True
        delay *= 2
    return False


async def confirm_persisted_then_run(
    message_bus: Any,
    memory_storage: MemoryStorage,
    storage: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
    cfg: MemoryAgentConfigRecord,
    global_cfg: MemoryGlobalConfig,
    *,
    reply_id: str | None,
    user_msg_id: str | None = None,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
) -> bool:
    """后台提取任务入口（middleware ``create_task`` / sweeper 直接调用）。

    锁外先做落库确认（``reply_id`` 非空时），再经
    ``message_bus.acquire_lock`` 排队进入锁内执行序列；返回是否成功保存。
    ``user_msg_id``（本轮 user 消息 id）仅供锁内 epoch 兜底重置定位
    游标使用（静默路径为 None）。
    """
    settings = get_settings()
    confirmed = True
    if reply_id is not None:
        confirmed = await _confirm_reply_persisted(
            storage,
            user_id,
            session_id,
            reply_id,
            settings.dangling_wait_secs,
        )
    async with message_bus.acquire_lock(
        _lock_key(user_id, agent_id, session_id),
        ttl_secs=settings.lock_ttl_secs,
    ):
        return await _run_extract_locked(
            memory_storage,
            storage,
            user_id,
            agent_id,
            session_id,
            cfg,
            global_cfg,
            reply_id=reply_id,
            user_msg_id=user_msg_id,
            confirmed=confirmed,
            retry_delays=retry_delays,
        )


async def _run_extract_locked(
    memory_storage: MemoryStorage,
    storage: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
    cfg: MemoryAgentConfigRecord,
    global_cfg: MemoryGlobalConfig,
    *,
    reply_id: str | None,
    user_msg_id: str | None = None,
    confirmed: bool,
    retry_delays: tuple[float, ...] = _DEFAULT_RETRY_DELAYS,
) -> bool:
    """锁内执行序列（共 5 步）；返回是否成功保存。

    步骤 1.5 为锁内兜底：锁内重读全局配置——开关关闭 skip；
    epoch 落后则就地惰性重置（middleware 未及收敛的静默路径 /
    重置失败 / 在飞快照三类场景）。
    """
    # 1. 会话状态——None 即已清理（sweeper 超龄/清理路径）
    state = await memory_storage.get_memory_session_state(
        user_id, agent_id, session_id
    )
    if state is None:
        logger.info(
            "memory: extract skip (session state gone) session=%s", session_id
        )
        return False

    # 1.5 锁内兜底：开关 + epoch（实时真源，middleware 快照滞后的补救）
    cfg_now = await memory_storage.get_memory_config(agent_id)
    if cfg_now is None:
        logger.info(
            "memory: extract skip (config gone) session=%s", session_id
        )
        return False
    cfg = cfg_now
    if not cfg.extraction_enabled:
        logger.info(
            "memory: extract skip (extraction disabled) session=%s",
            session_id,
        )
        return False
    if state.switch_epoch != cfg.switch_epoch:
        # epoch 落后 → 惰性重置（对齐点按路径区分）
        if reply_id is not None:
            cursor = await locate_cursor_before_message(
                storage, user_id, session_id, user_msg_id
            )
        else:
            # 静默路径：无新 epoch 内容 → 对齐最新消息（窗口空自然 skip）
            latest = await _load_recent_messages(
                storage, user_id, session_id, want=1
            )
            cursor = _msg_pos(latest[-1]) if latest else (
                state.cursor_created_at,
                state.cursor_msg_id,
            )
        if cursor is GIVE_UP:
            logger.info(
                "memory: epoch reset cursor locate gave up session=%s",
                session_id,
            )
            return False
        empty_cursor = cursor is None
        cursor_created_at, cursor_msg_id = (
            ("", "") if empty_cursor else cursor
        )
        state = await memory_storage.reset_memory_session_state(
            user_id,
            agent_id,
            session_id,
            cursor_created_at=cursor_created_at,
            cursor_msg_id=cursor_msg_id,
            epoch=cfg.switch_epoch,
            now=time.time(),
        )
        logger.info(
            "memory: epoch reset session=%s epoch=%d empty_cursor=%s",
            session_id,
            cfg.switch_epoch,
            empty_cursor,
        )
        # 不提前 return——继续步骤 2：轮数路径窗口 = 本轮起；
        # 静默路径窗口空，由"empty window"分支自然 skip

    # 2. 读窗口（条数 + token 双层上限，截断告警）；空窗口 = 游标已最新
    messages = await _read_window(
        storage, user_id, session_id, _cursor_of(state)
    )
    if not messages:
        logger.info(
            "memory: extract skip (empty window) session=%s", session_id
        )
        return False
    budgeted = _truncate_to_budget(messages, global_cfg.max_tokens)
    if len(budgeted) < len(messages):
        logger.warning(
            "memory: window truncated by token budget session=%s "
            "dropped=%d/%d",
            session_id,
            len(messages) - len(budgeted),
            len(messages),
        )

    # 3. 完整轮切片 + last_msg_id（本批末条被发送 assistant 消息 id）
    payload, last_msg = _slice_complete_turns(budgeted)
    if not payload or last_msg is None:
        logger.info(
            "memory: no complete turn to extract session=%s window=%d",
            session_id,
            len(budgeted),
        )
        return False
    last_msg_id = str(last_msg.id)

    # 4. 平台落库（重试退避）
    request = build_save_request(
        cfg,
        user_id,
        [(m["role"], m["content"]) for m in payload],
        last_msg_id,
    )
    await _save_with_retry(request, retry_delays)

    # 5. 先平台后游标：游标推进到本批末条（与已落库内容严格对齐）。
    #    本批若未含本轮回复（未确认且末条 != reply_id），说明尾部那一轮
    #    还没落库、不属于本批 → turns 置 1 而非清零：保留「>= 1」的触发
    #    动力（静默扫描候选 + 下轮累计），让后续触发把该轮补齐。
    last_created, last_id = _msg_pos(last_msg)
    await memory_storage.set_memory_session_cursor(
        user_id, agent_id, session_id, last_created, last_id
    )
    pending = (
        reply_id is not None and not confirmed and last_msg_id != reply_id
    )
    try:
        await memory_storage.reset_memory_session_turns(
            user_id, agent_id, session_id, turns=1 if pending else 0
        )
    except Exception:
        logger.warning(
            "memory: reset turns failed (cursor advanced; re-trigger only) "
            "session=%s",
            session_id,
        )
    if pending:
        logger.info(
            "memory: reply %s not in batch (cursor advanced; turns=1 awaiting "
            "re-extract) session=%s",
            reply_id,
            session_id,
        )
    logger.info(
        "memory: extracted session=%s messages=%d last_msg_id=%s cursor=%s",
        session_id,
        len(payload),
        last_msg_id,
        last_id,
    )
    return True


async def _save_with_retry(
    request: Any,
    retry_delays: tuple[float, ...],
) -> None:
    """save_memories 失败重试（退避默认 1s/3s，共 3 击）。"""
    attempt = 0
    while True:
        try:
            await pf.save_memories(request)
            return
        except pf.MemoriesSdkError:
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


__all__ = ["confirm_persisted_then_run"]
