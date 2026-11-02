# -*- coding: utf-8 -*-
"""记忆包数据模型：两个 Record + 全局配置。

命名三位一体：类名去 Record 后缀取 snake_case 复数即 SQL 表名——
MemoryAgentConfigRecord ↔ memory_agent_configs、MemorySessionStateRecord ↔
memory_session_states。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

#: 会话心跳（``last_active_at``）的统一时区：UTC，无夏令时 → 固定偏移，
#: 与框架 ``_sql`` 的 naive UTC 约定一致。存储侧据此把 epoch 秒写成
#: ``DATETIME`` 字面值，展示侧据此还原可读时间——单一来源，保证两侧偏移
#: 一致。
HEARTBEAT_TZ = timezone.utc


class MemoryAgentConfigRecord(BaseModel):
    """agent 级记忆配置（表 memory_agent_configs，主键 agent_id）。

    不含 user_id：归属由框架的 agent 表提供，本表只按 agent_id 存取。
    ``infer`` 默认 "1"=提取，按字符串存储、由 mapping 适配为 SDK 的 int 形参。

    双开关：

    - ``injection_enabled``：记忆注入（检索 + 注入上下文）；
    - ``extraction_enabled``：记忆提取（计数 / 触发 / sweeper / 心跳）；
    - ``switch_epoch``：提取开关版本号——仅 ``extraction_enabled`` 变化时
      由服务端 +1（routers PUT），用于会话状态的惰性重置判定；客户端
      传入一律被覆盖。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    injection_enabled: bool = False
    extraction_enabled: bool = False
    switch_epoch: int = 0
    memory_type: str = "0"
    update_rounds: int = Field(default=10, ge=1)
    memory_prompt: str = ""
    top_k: int = Field(default=5, ge=1)
    agent_plat: int = 0
    message_type: int = 0
    infer: str = "1"                      
    caller: str = ""                      # 注册返回值，只存不发


class MemorySessionStateRecord(BaseModel):
    """会话记忆状态（表 memory_session_states，自增 id 主键 + 身份唯一索引）。

    身份三元组（user_id, agent_id, session_id）由唯一索引
    UK1_memory_session_states 保证；agent_id / last_active_at 建组合索引
    I1_memory_session_states（agent 等值 + 心跳范围）；user_id / session_id
    为身份列冗余存储（免解析），无独立查询路径，不单独建索引。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    agent_id: str
    session_id: str
    # 必填：SQL 后端的 payload 不含它（值为提升列的真源），读路径必须由列
    # 拼回；漏拼即 fail-fast，避免静默取 0（0 会让 sweeper 判该会话超龄并清）
    last_active_at: float
    # 必填：同上（列是真源，payload 剔除）。若给默认 0，漏 merge 会静默通过
    # → 该会话永远不满足 `turns >= 1` → 提取再也不触发（最隐蔽的故障形态）
    turns: int
    cursor_created_at: str = ""
    cursor_msg_id: str = ""
    switch_epoch: int = 0

    @property
    def record_id(self) -> str:
        return f"{self.user_id}:{self.agent_id}:{self.session_id}"

    @property
    def last_active_dt(self) -> datetime:
        """心跳的可读时间（UTC）；仅用于日志与展示，不入 payload。"""
        return datetime.fromtimestamp(self.last_active_at, tz=HEARTBEAT_TZ)


__all__ = [
    "HEARTBEAT_TZ",
    "MemoryAgentConfigRecord",
    "MemorySessionStateRecord",
]
