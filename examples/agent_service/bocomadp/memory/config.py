# -*- coding: utf-8 -*-
"""全局记忆配置（MemoryRuntimeConfig）：runtime_configs 表 key=memory，缺失用默认。

与 summarization 等配置段同模式：真源在 PG ``runtime_configs`` 表（可经
``/config/memory`` 热更新），无记录 / 字段缺失 / 读失败时回退代码默认值。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from bocomadp.runtime_config_store import config_get

# runtime_configs 表中本配置段的 key
RUNTIME_CONFIG_KEY = "memory"


class MemoryRuntimeConfig(BaseModel):
    """记忆后台运行参数（全部带默认值：无记录时使用代码默认）。"""

    default_memory_prompt: str = Field(
        default="",
        description=(
            "默认记忆提示词（全局维度）：供未单独配置 memory_prompt 的智能体"
            "作为缺省值参考；当前仅存储与可读，不参与内部处理逻辑。"
        ),
    )

    idle_minutes: int = Field(
        default=15,
        ge=1,
        description="会话静默（无对话心跳）多少分钟后可被清扫提取。",
    )
    sweep_interval_seconds: int = Field(
        default=60,
        ge=1,
        description="静默会话后台扫描间隔（秒）。",
    )
    max_tokens: int = Field(
        default=90000,
        ge=1,
        description="单会话单次提取送入模型的 token 预算（超长按字节/4 截断）。",
    )


async def get_memory_runtime_config() -> MemoryRuntimeConfig:
    """读取全局记忆运行配置；无记录 / 字段非法时返回默认值。

    ``runtime_config_store.config_get`` 为 async（真源在 PG），因此本函数
    也保持 async，由调用方（async 路由 / 后台任务 / 中间件装配）直接 await。
    """
    payload = await config_get(RUNTIME_CONFIG_KEY)
    if not payload:
        return MemoryRuntimeConfig()
    try:
        return MemoryRuntimeConfig(**payload)
    except ValidationError:  # 反序列化失败视为无有效配置
        return MemoryRuntimeConfig()


__all__ = ["MemoryRuntimeConfig", "get_memory_runtime_config", "RUNTIME_CONFIG_KEY"]
