# -*- coding: utf-8 -*-
"""全局记忆配置。

真源在目标项目通用 kv 表 ``runtime_configs``（``config_key='memory'``），
读写走 :mod:`bocomadp.memory.global_config_store`（内部委托
``bocomadp.runtime_config_store``；读宽松 / 写严格），可经
``PUT /memory/config`` 热更新；无记录 / 字段缺失 / 读失败时回退代码默认值。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# runtime_configs 表中本配置段的 key
GLOBAL_CONFIG_KEY = "memory"


class MemoryGlobalConfig(BaseModel):
    """记忆后台运行参数（全部带默认值：无记录时使用代码默认）。"""

    idle_minutes: int = Field(
        default=60,
        ge=1,
        description="会话静默（无对话心跳）多少分钟后可被清扫提取。",
    )

    sweep_interval_seconds: int = Field(
        default=300,
        ge=1,
        description="静默会话后台扫描间隔（秒）。",
    )

    max_tokens: int = Field(
        default=90000,
        ge=1,
        description="单会话单次提取送入模型的 token 预算（超长按字节/4 截断）。",
    )

    state_ttl_days: int = Field(
        default=7,
        ge=1,
        description=(
            "记忆运行时状态保留天数：静默扫描器据此修剪超龄会话——最后活跃"
            "距今达到该天数的会话，其会话状态 / 轮数计数 / 提取游标一并清除"
            "（视为遗忘，下次对话从 1 重新计数、重建游标）。"
        ),
    )

    @property
    def state_ttl_secs(self) -> int:
        """超龄修剪窗口（秒）——由 :attr:`state_ttl_days` 换算，供扫描器使用。"""
        return self.state_ttl_days * 86400


__all__ = ["MemoryGlobalConfig", "GLOBAL_CONFIG_KEY"]
