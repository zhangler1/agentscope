# -*- coding: utf-8 -*-
"""会话级记忆中间件：每轮检索注入 + 计数 + 轮数触发提取。

时序（on_reply）：
1. 进入回复时若输入含 user 消息，取 query 检索记忆（平台 searchMemory，
   请求经 mapping 构造——单一真源）；
2. 透传 ``next_handler`` 事件流，``ReplyStartEvent`` 后把命中结果追加为
   一条 ``name=="memory"`` 消息（**不清理历史记忆消息**，仿框架 mem0：
   逐轮累积，跨轮相关旧记忆后续仍可引用；token 增长交给压缩兜底）；
3. 回复**正常完成**（无异常）才收敛状态：
   epoch 检测——状态不存在 → ``ensure`` 建档（游标 = 本轮 user 前一条，
   **唯一**建档入口）；状态存在但 ``switch_epoch`` 落后 → 就地重置
   （重开后的首个用户轮，此刻“前一条”恰为开关边界）→ ``touch`` 心跳
   → ``increment``；``turns >= update_rounds`` 即触发后台提取
   （``>=`` 判定：提取失败 / 预检放弃后下一轮即重试，不必等下一个
   整数倍）。

触发（_maybe_trigger）：``bus.is_locked`` 预检——已锁本轮放弃（turns 已
累计、游标未动，下轮必然再触发，不丢数据）；未锁 ``create_task`` 后台
执行 ``confirm_persisted_then_run``（携带本轮 reply_id + user_msg_id，
锁外落库确认），任务登记 ``_pending_extract``（install_memory 关闭时
gather 收尾）。

on_reasoning：每次推理迭代刷新心跳（touch 只更新，不建档），供静默扫描
判断活跃；非用户轮不做 epoch 重置（无 user 锚点可定位游标）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agentscope.event import ReplyStartEvent
from agentscope.message import AssistantMsg, HintBlock, Msg
from agentscope.middleware import MiddlewareBase

from . import mapping
from . import platform as pf
from .config import MemoryGlobalConfig
from .extractor import (
    GIVE_UP,
    _lock_key,
    confirm_persisted_then_run,
    locate_cursor_before_message,
)
from .storage._models import MemoryAgentConfigRecord
from .storage._port import MemoryStorage

logger = logging.getLogger("as")

# 注入消息的 name 标记：检索结果以 role=assistant/name=memory 的消息注入，
# 内容放在 HintBlock 里（formatter 会在调用模型前把它转成一条 user 消息）；
# 命中时逐轮追加（不清理历史，仿框架 mem0），本标记便于识别与上层清理。
MEMORY_MESSAGE_NAME = "memory"

#: 后台提取任务登记表：fire-and-forget + done 回调摘除；
#: install_memory 关闭时 gather(return_exceptions=True) 收尾。
_pending_extract: set[asyncio.Task] = set()


class MemoryMiddleware(MiddlewareBase):
    """每轮检索注入 + 计数 + 轮数触发提取。"""

    def __init__(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        cfg: MemoryAgentConfigRecord,
        global_cfg: MemoryGlobalConfig | None = None,
        *,
        memory_storage: MemoryStorage,
        storage: Any,
        message_bus: Any,
    ) -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self._cfg = cfg
        self._rt_cfg = global_cfg or MemoryGlobalConfig()
        self._memory_storage = memory_storage
        self._storage = storage
        self._message_bus = message_bus
        if not self._cfg.caller:
            # caller 缺失（平台未注册 / 注册失败）会让检索被整体跳过：
            # 构造期告警一次，避免只在运行时表现为"记忆无声缺席"。
            logger.warning(
                "memory: caller 缺失（平台注册未完成/失败），"
                "注入检索将全部跳过 agent_id=%s session=%s",
                self.agent_id,
                self.session_id,
            )

    # --------------------------------------------------------------- hooks

    async def on_reply(self, agent: Any, input_kwargs: dict, next_handler):
        """记忆检索注入 + 透传 + 回复完成后状态收敛/计数/触发。"""
        query, user_msg_id = self._extract_user_input(input_kwargs)
        memories = await self._retrieve_safe(query) if query else []
        async for event in next_handler(**input_kwargs):
            if isinstance(event, ReplyStartEvent) and memories:
                self._inject_memories(agent, memories)
                memories = []
            yield event
        # 回复正常结束才收敛状态（异常中断不计、不触发）
        await self._record_turn(agent, query, user_msg_id)

    async def on_reasoning(self, agent: Any, input_kwargs: dict, next_handler):
        """每次推理迭代刷新活跃心跳（touch 落库，供静默扫描判断）。"""
        try:
            await self._memory_storage.touch_memory_session(
                self.user_id, self.agent_id, self.session_id, now=time.time()
            )
        except Exception:
            logger.debug(
                "memory: heartbeat failed session=%s", self.session_id
            )
        async for event in next_handler(**input_kwargs):
            yield event

    # ------------------------------------------------------- 检索与注入

    def _can_retrieve(self) -> bool:
        """能否发起平台检索：开关开启且已完成平台注册（caller）。

        无 caller（注册未完成的 pending 窗口期、框架日志、子智能体
        批量回复等）时纯透传计数——避免平台侧拿不到 userCode 语义的
        检索。
        """
        if not self._cfg.injection_enabled:
            return False
        return bool(self._cfg.caller)

    async def _retrieve(self, query: str) -> list[str]:
        """检索记忆：mapping 构造请求 → 平台 search_memory（单一真源）。"""
        request = mapping.build_search_request(self._cfg, self.user_id, query)
        items = await pf.search_memory(request)
        return [self._item_to_text(it) for it in items]

    async def _retrieve_safe(self, query: str) -> list[str]:
        """检索失败不阻塞对话回合（降级为空注入）。"""
        if not self._can_retrieve():
            logger.debug(
                "memory: retrieve skipped session=%s "
                "(injection_enabled=%s, caller=%s)",
                self.session_id,
                self._cfg.injection_enabled,
                "set" if self._cfg.caller else "empty",
            )
            return []
        try:
            memories = await self._retrieve(query)
        except Exception:
            logger.exception(
                "memory: retrieve failed session=%s, skip injection",
                self.session_id,
            )
            return []
        if memories:
            # 待注入内容
            logger.debug(
                "memory: %d memories to inject session=%s query=%r:\n  - %s",
                len(memories),
                self.session_id,
                query,
                "\n  - ".join(memories),
            )
        else:
            logger.debug(
                "memory: retrieved 0 memories session=%s query=%r",
                self.session_id,
                query,
            )
        return memories

    @staticmethod
    def _item_to_text(item: Any) -> str:
        """平台返回项 → 注入文本：非 dict 或缺 memory 字段则原样 str。"""
        if isinstance(item, dict) and "memory" in item:
            return str(item["memory"])
        return str(item)

    def _inject_memories(self, agent: Any, memories: list[str]) -> None:
        """ReplyStart 后把记忆追加到本轮用户消息之后。

        容器用 ``role=assistant`` + ``HintBlock``：user 消息不能承载
        ``HintBlock``，formatter 会在调用模型前把它转成一条 user 消息
        （与框架 mem0 中间件同一手法）。

        落点：调用方在 ``ReplyStartEvent`` 后触发——该事件在用户输入写入
        ``agent.state.context`` 之后、推理循环之前产生，此刻 ``context[-1]``
        即本轮用户消息，故 append 正好紧随其后；助手回复消息随后由框架以
        ``reply_id`` 另建，不会被本条干扰。

        **不清理历史记忆消息**：仿框架 mem0 的追加注入——上轮注入的相关
        记忆在后续轮次仍有参考价值；token 增长由记忆链路自然节制（每轮
        命中数有限），不再引入清除副作用。
        """
        if not memories:
            return  # 空命中：不注入空消息（历史记忆原样保留）
        try:
            mem_msg = AssistantMsg(
                name=MEMORY_MESSAGE_NAME,
                content=[HintBlock(hint="\n".join(memories))],
            )
            agent.state.context.append(mem_msg)
            logger.debug(
                "memory: injected %d memories session=%s (context[-1])",
                len(memories),
                self.session_id,
            )
        except Exception:
            logger.exception(
                "memory: inject failed session=%s", self.session_id
            )

    # ------------------------------------------------------- 计数与触发

    async def _record_turn(
        self, agent: Any, user_query: str, user_msg_id: str | None = None
    ) -> None:
        """回复正常完成：状态收敛（epoch）+ 心跳 +（仅用户输入轮）轮数。

        ``user_query`` 为空表示本轮**不是用户消息发起的**（工具确认、外部
        执行结果、resume 续轮等），只刷新心跳、不计轮也不触发——
        ``update_rounds`` 统计的是"用户交互轮数"，续轮计入会让轮次虚高
        并撑大后续取数窗口。

        epoch 收敛（increment 之前）：

        - 状态不存在 → ``ensure`` 建档（游标 = 本轮 user 前一条），
          非用户轮不建档；
        - 状态存在但 ``switch_epoch`` 落后（开关曾关闭过）→ 就地重置，
          此刻"本轮 user 前一条"恰为开关边界；
        - ``touch`` 返回 None（状态恰被 TTL 清理的跨进程竞态）→ 本轮
          放弃——防止 increment 造出无游标状态。
        """
        reply_id = self._reply_id(agent)
        try:
            state = await self._memory_storage.get_memory_session_state(
                self.user_id, self.agent_id, self.session_id
            )
            if state is None:
                if not user_query:
                    return
                if not await self._ensure_session_state(user_msg_id):
                    return  # 定位放弃：本轮不计不建，下轮重试（安全侧）
            elif user_query and state.switch_epoch != self._cfg.switch_epoch:
                await self._reset_stale_session(user_msg_id)
            touched = await self._memory_storage.touch_memory_session(
                self.user_id, self.agent_id, self.session_id, now=time.time()
            )
            if touched is None:
                logger.info(
                    "memory: session state vanished mid-turn, "
                    "skip session=%s",
                    self.session_id,
                )
                return
            if not user_query:
                return
            if not self._cfg.extraction_enabled:
                # 不提取的 agent（只开注入）**不计数**：turns 是提取用的计数，
                # 累加会让这些会话永久满足 `turns >= 1` 却永远不会被提取，
                # 加 limit 之后它们会占掉提取候选名额。心跳已
                # 在上方刷新，处理到此为止。
                return
            turns = await self._memory_storage.increment_memory_session_turns(
                self.user_id, self.agent_id, self.session_id
            )
            hit = turns >= self._cfg.update_rounds
            logger.debug(
                "memory: turn threshold check session=%s reply_id=%s "
                "turns=%d update_rounds=%d hit=%s",
                self.session_id, reply_id, turns, self._cfg.update_rounds, hit,
            )
            if hit:
                await self._maybe_trigger(reply_id, user_msg_id)
        except Exception:
            logger.exception(
                "memory: record turn failed session=%s", self.session_id
            )

    async def _ensure_session_state(self, user_msg_id: str | None) -> bool:
        """建档（唯一创建入口）：游标 = 本轮 user 的前一条。

        返回是否建档成功；False = 定位放弃（窗口空等），本轮不计不建，
        下一用户轮重试——安全侧：宁可不提取，绝不回捞。
        """
        cursor = await locate_cursor_before_message(
            self._storage,
            self.user_id,
            self.session_id,
            user_msg_id,
        )
        if cursor is GIVE_UP:
            logger.info(
                "memory: ensure gave up (no cursor anchor) session=%s",
                self.session_id,
            )
            return False
        empty_cursor = cursor is None
        cursor_created_at, cursor_msg_id = (
            ("", "") if empty_cursor else cursor
        )
        created = await self._memory_storage.ensure_memory_session_state(
            self.user_id,
            self.agent_id,
            self.session_id,
            cursor_created_at=cursor_created_at,
            cursor_msg_id=cursor_msg_id,
            epoch=self._cfg.switch_epoch,
            now=time.time(),
        )
        logger.debug(
            "memory: session state ensured session=%s created=%s "
            "empty_cursor=%s",
            self.session_id, created, empty_cursor,
        )
        return True

    async def _reset_stale_session(self, user_msg_id: str | None) -> None:
        """epoch 落后 → 就地重置。

        游标 = 本轮 user 的前一条——重置必然发生在重开后的第一个
        用户轮，此刻"前一条"恰为开关边界（关闭期最后一条消息），
        本轮起的内容全部落在游标之后。定位放弃时 epoch 留给锁内
        兜底收敛。
        """
        cursor = await locate_cursor_before_message(
            self._storage,
            self.user_id,
            self.session_id,
            user_msg_id,
        )
        if cursor is GIVE_UP:
            logger.info(
                "memory: epoch reset gave up (no cursor anchor) session=%s",
                self.session_id,
            )
            return
        empty_cursor = cursor is None
        cursor_created_at, cursor_msg_id = (
            ("", "") if empty_cursor else cursor
        )
        await self._memory_storage.reset_memory_session_state(
            self.user_id,
            self.agent_id,
            self.session_id,
            cursor_created_at=cursor_created_at,
            cursor_msg_id=cursor_msg_id,
            epoch=self._cfg.switch_epoch,
            now=time.time(),
        )
        logger.info(
            "memory: epoch reset (middleware) session=%s epoch=%d "
            "empty_cursor=%s",
            self.session_id, self._cfg.switch_epoch, empty_cursor,
        )

    async def _maybe_trigger(
        self, reply_id: str, user_msg_id: str | None
    ) -> None:
        """is_locked 预检 → 未锁 create_task 后台提取（不阻塞对话回合）。

        已锁 → 本轮放弃（turns 已累计、游标未动——``>=`` 语义保证下轮
        必然再触发，不丢数据）。
        """
        key = _lock_key(self.user_id, self.agent_id, self.session_id)
        if await self._message_bus.is_locked(key):
            return
        task = asyncio.create_task(
            confirm_persisted_then_run(
                self._message_bus,
                self._memory_storage,
                self._storage,
                self.user_id,
                self.agent_id,
                self.session_id,
                self._cfg,
                self._rt_cfg,
                reply_id=reply_id,
                user_msg_id=user_msg_id,
            )
        )
        _pending_extract.add(task)
        task.add_done_callback(_pending_extract.discard)

    # ------------------------------------------------------------- utils

    @staticmethod
    def _extract_user_input(input_kwargs: dict) -> tuple[str, str | None]:
        """从回复入参提取 ``(用户 query, 本轮 user 消息 id)``。

        框架 ``on_reply`` 契约传 ``inputs``（``Msg | list[Msg] | 事件 |
        None``，agentscope ``middleware/_base.py``）——按 ``msg`` 键读取
        会恒为空串。非用户轮（事件对象 / ``None`` / 无非空文本的
        user 消息）→ ``("", None)``：只刷新心跳，不计轮不触发。
        """
        inputs = input_kwargs.get("inputs")
        if isinstance(inputs, Msg):
            candidates = [inputs]
        elif isinstance(inputs, (list, tuple)):
            candidates = [m for m in inputs if isinstance(m, Msg)]
        else:
            return "", None
        for msg in reversed(candidates):
            if msg.role == "user":
                text = (msg.get_text_content() or "").strip()
                if text:
                    return text, str(msg.id)
        return "", None

    @staticmethod
    def _reply_id(agent: Any) -> str:
        """本轮回复 id（提取锁外落库确认的锚点）。"""
        reply_id = getattr(getattr(agent, "state", None), "reply_id", "-")
        return str(reply_id or "-")


__all__ = [
    "MemoryMiddleware",
    "MEMORY_MESSAGE_NAME",
]
