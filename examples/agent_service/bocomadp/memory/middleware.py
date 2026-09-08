# -*- coding: utf-8 -*-
"""会话级记忆中间件：每轮检索注入 + 计数 + 轮数触发提取。

时序（on_reply）：
1. 进入回复时若输入含 user 消息，取 query 检索记忆（平台 searchMemory）；
2. 透传 ``next_handler`` 事件流，``ReplyStartEvent`` 后把命中结果追加为
   一条 ``name=="memory"`` 消息（**不清理历史记忆消息**，仿框架 mem0：
   逐轮累积，跨轮相关旧记忆后续仍可引用；token 增长交给压缩兜底）；
3. 回复**正常完成**（无异常）才计数：mark_active + incr_turn；达到
   ``update_rounds`` 倍数则抢提取锁并触发后台提取。

on_reasoning：每次推理迭代刷新心跳（mark_active），供扫描器判断活跃。

外部依赖（redis / 检索 / 触发）通过构造注入：默认均为 None 时中间件为
纯透传（计数/检索/触发全关），便于单元测试与装配点灵活接线。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from agentscope.event import ReplyStartEvent
from agentscope.message import AssistantMsg, Msg
from agentscope.middleware import MiddlewareBase

from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.state import incr_turn, mark_active, try_acquire_lock
from bocomadp.memory.store import MemoryConfig

logger = logging.getLogger("as")

# 注入消息的 name 标记：检索结果以 role=assistant/name=memory 的消息注入；
# 命中时逐轮追加（不清理历史，仿框架 mem0），本标记便于识别与上层清理。
MEMORY_MESSAGE_NAME = "memory"

# 检索回调：async (keyword: str) -> list[str]（已归一化的记忆文本列表）
SearchFn = Callable[[str], Awaitable[list[str]]]
# 触发回调：async (turns: int) -> None（内部自行做后台任务/日志兜底）
TriggerFn = Callable[[int], Awaitable[None]]


class MemoryMiddleware(MiddlewareBase):
    """每轮检索注入 + 计数 + 轮数/触发提取（核心时序见 spec §8/§9）。"""

    def __init__(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        memory_config: MemoryConfig,
        runtime_config: MemoryRuntimeConfig | None = None,
        *,
        redis=None,
        search: SearchFn | None = None,
        trigger: TriggerFn | None = None,
    ) -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self._cfg = memory_config
        self._rt_cfg = runtime_config or MemoryRuntimeConfig()
        self._redis = redis
        self._search = search
        self._trigger = trigger

    # ------------------------------------------------------------------
    # on_reply —— 检索注入 + 计数 + 触发
    # ------------------------------------------------------------------

    async def on_reply(self, agent: Any, input_kwargs: dict, next_handler: Any):
        """检索注入（user 输入轮）+ 透传 + 正常完成后计数/触发。"""
        reply_id = self._reply_id(agent)
        memories: list[str] = []
        query = self._extract_user_query(input_kwargs.get("inputs"))
        if query and self._can_retrieve():
            try:
                memories = await self._retrieve(query)
                logger.info(
                    "memory: retrieved session=%s reply_id=%s hits=%d "
                    "(keyword_len=%d, top_k=%d)",
                    self.session_id,
                    reply_id,
                    len(memories),
                    len(query),
                    self._cfg.top_k,
                )
            except Exception:  # noqa: BLE001 — 检索失败不影响回复
                logger.warning(
                    "memory: retrieve failed session=%s reply_id=%s "
                    "(skip inject)",
                    self.session_id,
                    reply_id,
                    exc_info=True,
                )
                memories = []

        completed = False
        injected = False
        try:
            async for evt in next_handler(**input_kwargs):
                # ReplyStartEvent 后追加注入（不删历史记忆）：此时模型输入
                # 尚未构建（on_reasoning 内才 _prepare_model_input），注入对
                # 模型可见。
                if memories and not injected and isinstance(evt, ReplyStartEvent):
                    try:
                        self._inject_memories(agent, memories)
                    except Exception:  # noqa: BLE001 — 注入失败不影响回复
                        logger.warning(
                            "memory: inject failed session=%s reply_id=%s",
                            self.session_id,
                            reply_id,
                            exc_info=True,
                        )
                    injected = True
                yield evt
            completed = True
        finally:
            if completed:
                await self._record_turn(agent)

    def _can_retrieve(self) -> bool:
        """记忆开关开启 且（已有 caller 走平台 或 注入检索回调）。"""
        if not self._cfg.memory_enabled:
            return False
        return self._search is not None or bool(self._cfg.caller)

    def _extract_user_query(self, inputs: Any) -> str:
        """从本轮回复输入中提取最新 user 消息文本；非 user 轮返回空。"""
        msgs: list[Msg] = []
        if isinstance(inputs, Msg):
            msgs = [inputs]
        elif isinstance(inputs, list):
            msgs = [m for m in inputs if isinstance(m, Msg)]
        for msg in reversed(msgs):
            if getattr(msg, "role", None) == "user":
                text = msg.get_text_content()
                if text:
                    return text
        return ""

    async def _retrieve(self, query: str) -> list[str]:
        """检索记忆：优先注入的 search 回调（装配点接线平台封装）。"""
        if self._search is not None:
            return await self._search(query)
        # 默认走平台 searchMemory（caller 非空才有意义；无检索回调时到达此处
        # 表示 cfg.caller 非空）。
        from bocomadp.memory import platform as pf

        param = {
            "caller": self._cfg.caller,
            "agentName": self.agent_id,
            "userCode": self.user_id,
            "keyword": query,
            "agentId": self.agent_id,
            "agentPlat": self._cfg.agent_plat,
            "topK": self._cfg.top_k,
            "extraParams": {},
        }
        items = await pf.search_memory(param)
        return [self._item_to_text(it) for it in items]

    @staticmethod
    def _item_to_text(item: dict[str, Any]) -> str:
        """平台检索条目 → 文本（优先 memory/content 字段，否则整体串化）。"""
        if not isinstance(item, dict):
            return str(item)
        for key in ("memory", "content", "text", "summary"):
            value = item.get(key)
            if value:
                return str(value)
        return str(item)

    # ------------------------------------------------------------------
    # 注入（追加式，仿框架 mem0：命中追加、不清理历史记忆消息）
    # ------------------------------------------------------------------

    def _inject_memories(self, agent: Any, memories: list[str]) -> None:
        """把本轮检索结果以一条 name=memory 消息追加到 context 末尾。

        仿框架 mem0 中间件语义：**不删除历史记忆消息**——每次检索命中都
        追加一条，逐轮累积，跨轮相关的旧记忆在后续轮次仍可被模型引用；
        未命中时调用方不会调用本方法，旧记忆保持不动。记忆消息随轮次
        累积带来的 token 增长交由上层上下文压缩（compress_context）兜底。
        """
        if not memories:
            return
        text = "\n".join(f"- {item}" for item in memories)
        # ReplyStartEvent 后调用：context 末尾恰是刚进入的本轮 user 消息，
        # append 即落在 user 之后、本轮 assistant 回复之前（与 mem0 一致）。
        agent.state.context.append(
            AssistantMsg(name=MEMORY_MESSAGE_NAME, content=text),
        )

    # ------------------------------------------------------------------
    # 计数 / 触发
    # ------------------------------------------------------------------

    @staticmethod
    def _reply_id(agent: Any) -> str:
        """当前回复 id（每轮回复唯一，用于跨日志关联一次回复的检索/注入/计数）。"""
        state = getattr(agent, "state", None)
        return getattr(state, "reply_id", "-") or "-"

    async def _record_turn(self, agent: Any) -> None:
        """回复正常完成：心跳 + 轮数 +1，达阈值抢锁触发提取。"""
        reply_id = self._reply_id(agent)
        if self._redis is None:
            return
        try:
            await mark_active(self._redis, self.session_id)
            turns = await incr_turn(self._redis, self.session_id)
            if self._should_trigger(turns):
                await self._maybe_trigger(turns, reply_id)
        except Exception:  # noqa: BLE001 — 计数/触发失败不影响回复
            logger.warning(
                "memory: record turn failed session=%s reply_id=%s",
                self.session_id,
                reply_id,
                exc_info=True,
            )

    def _should_trigger(self, turns: int) -> bool:
        """turns 达到 update_rounds 的整数倍（update_rounds>=1，禁止纯静默）。"""
        return self._cfg.update_rounds >= 1 and turns % self._cfg.update_rounds == 0

    async def _maybe_trigger(self, turns: int, reply_id: str = "-") -> None:
        """抢锁成功后触发后台提取；无触发回调则不做事（锁保留防重复）。"""
        if not await try_acquire_lock(self._redis, self.session_id):
            return
        if self._trigger is None:
            return
        try:
            await self._trigger(turns)
        except Exception:  # noqa: BLE001 — 提取失败下次再触发
            logger.warning(
                "memory: extract trigger failed session=%s reply_id=%s",
                self.session_id,
                reply_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # on_reasoning —— 心跳
    # ------------------------------------------------------------------

    async def on_reasoning(self, agent: Any, input_kwargs: dict, next_handler: Any):
        """每次推理迭代刷新活跃心跳（供静默扫描判断）。"""
        reply_id = self._reply_id(agent)
        if self._redis is not None:
            try:
                await mark_active(self._redis, self.session_id)
            except Exception:  # noqa: BLE001 — 心跳失败静默
                logger.debug(
                    "memory: heartbeat failed session=%s reply_id=%s",
                    self.session_id,
                    reply_id,
                )
        async for evt in next_handler(**input_kwargs):
            yield evt


__all__ = [
    "MemoryMiddleware",
    "MEMORY_MESSAGE_NAME",
    "SearchFn",
    "TriggerFn",
]
