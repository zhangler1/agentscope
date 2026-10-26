# -*- coding: utf-8 -*-
"""MemoryStorage 抽象端口：记忆包自有数据的唯一存取界面。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ._models import MemoryAgentConfigRecord, MemorySessionStateRecord


class MemoryStorage(ABC):
    """15 个方法：agent 配置 4 + 会话状态 11。"""

    # --- agent 级配置 ---
    @abstractmethod
    async def upsert_memory_config(self, record: MemoryAgentConfigRecord) -> MemoryAgentConfigRecord: ...
    @abstractmethod
    async def get_memory_config(self, agent_id: str) -> MemoryAgentConfigRecord | None: ...
    @abstractmethod
    async def list_memory_configs(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[MemoryAgentConfigRecord]:
        """按 ``agent_id`` 升序分页列出 agent 配置。

        ``limit`` 默认 100（与送审 SQL 的 ``LIMIT`` 一致，避免无条件全表
        扫描）；取全量由调用方叠加 ``offset`` 分页。
        """
    @abstractmethod
    async def delete_memory_config(self, agent_id: str) -> bool: ...

    # --- 会话状态 ---
    @abstractmethod
    async def touch_memory_session(
        self, user_id: str, agent_id: str, session_id: str, now: float
    ) -> MemorySessionStateRecord | None:
        """心跳：**只更新已存在状态**的 last_active_at（不动 turns/cursor）。

        状态不存在 → no-op 返回 None（**不创建**——建档由
        :meth:`ensure_memory_session_state` 唯一负责，避免产生无游标
        状态导致提取回捞旧消息）。
        """
    @abstractmethod
    async def ensure_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> bool:
        """**唯一**的状态创建入口：不存在才插入（带初始游标 + epoch +
        turns=0）；已存在 no-op 返回 False（不覆盖）。

        返回 True 表示由本次创建（心跳已随建档写入）。
        """
    @abstractmethod
    async def reset_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> MemorySessionStateRecord:
        """无条件重写（upsert）：turns=0 + 新游标 + 新 epoch + 心跳。

        惰性重置的写入口（middleware epoch 检测 / 锁内兜底共用）；
        定义为 upsert 覆盖"重置瞬间状态恰被 TTL 清理"的竞态。
        """
    @abstractmethod
    async def increment_memory_session_turns(self, user_id: str, agent_id: str, session_id: str) -> int: ...
    @abstractmethod
    async def get_memory_session_state(self, user_id: str, agent_id: str, session_id: str) -> MemorySessionStateRecord | None: ...
    @abstractmethod
    async def set_memory_session_cursor(self, user_id: str, agent_id: str, session_id: str, created_at: str, msg_id: str) -> None: ...
    @abstractmethod
    async def reset_memory_session_turns(self, user_id: str, agent_id: str, session_id: str, turns: int = 0) -> None: ...
    @abstractmethod
    async def delete_memory_session_state(self, user_id: str, agent_id: str, session_id: str) -> bool: ...
    @abstractmethod
    async def delete_stale_session_states(
        self, last_active_before: float, limit: int
    ) -> int:
        """批量删除心跳早于 ``last_active_before`` 的会话状态（每批 ≤ ``limit``）。

        返回**本批处理的候选条数**（≤ ``limit``）——不是"删除行数"：SQL 侧
        并发跳过、Redis 侧 Hash 已被 TTL 回收（悬空成员只回收索引）都会让
        "删除行数"小于候选数，而调用方要靠返回值判断"还有没有下一批"。

        两个参数均无默认值——阈值与上限必须显式传入，禁止无界删除。语义：
        条件在删除时求值（两后端一致）；心跳**恰等于阈值不算超龄**（严格
        ``<``）。
        """
    @abstractmethod
    async def list_memory_session_states(
        self,
        *,
        last_active_before: float,
        limit: int | None = None,
    ) -> list[MemorySessionStateRecord]:
        """列举提取候选：**已静默且仍有未提取轮次**（``turns >= 1``）的会话状态。

        仅服务 sweeper 的静默补提取。含义由两个条件界定：
        ``last_active_before``（心跳上界 = 静默够久；**必填**，故不存在
        "无条件全表扫描"入口）与 ``turns >= 1``（有内容待提取，由实现固化）。

        - SQL：``WHERE last_active_at < ? AND turns >= 1 ORDER BY last_active_at``
          + ``limit``（"最久静默优先"，防每轮取到同一批造成饥饿）；
        - Redis：``pending`` 副索引一次 ``ZRANGEBYSCORE`` 精确命中，不读无关 Hash。

        候选 ≠ 可提取：agent 是否开启提取、游标之后是否有新消息、``switch_epoch``
        是否一致由调用方逐条判定（sweeper 的 ``enabled`` 过滤 + 提取流程）。
        """
    @abstractmethod
    async def delete_agent_session_states(self, agent_id: str) -> int:
        """删除某 agent 的**全部**会话状态，返回处理条数。
        """


__all__ = ["MemoryStorage"]
