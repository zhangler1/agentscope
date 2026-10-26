# -*- coding: utf-8 -*-
"""全局记忆参数薄封装：走 ``bocomadp.runtime_config_store`` + 默认值回退。

目标项目不使用记忆包自带的 ``global_configs`` 表：全局记忆参数与
``summarization`` 等配置段一样，统一落 ``runtime_configs`` 表
（``config_key='memory'``），可经 ``/api/config/memory`` 通用接口读写。

读侧宽松（sweeper / middleware 高频路径）：无记录 / 读取或反序列化失败
→ 代码默认值；写侧 fail-fast（异常向上抛，由路由层转 5xx）。
"""
from __future__ import annotations

from bocomadp.runtime_config_store import (
    config_delete,
    config_set,
    get_typed_config,
)

from .config import GLOBAL_CONFIG_KEY, MemoryGlobalConfig


async def get_memory_global_config() -> MemoryGlobalConfig:
    """读全局记忆参数；任何失败路径回退代码默认值。"""
    got = await get_typed_config(GLOBAL_CONFIG_KEY, MemoryGlobalConfig)
    return got if got is not None else MemoryGlobalConfig()


async def upsert_memory_global_config(
    config: MemoryGlobalConfig,
) -> MemoryGlobalConfig:
    """写全局记忆参数（runtime_configs 表 UPSERT）；失败向上抛。"""
    await config_set(GLOBAL_CONFIG_KEY, config.model_dump())
    return config


async def delete_memory_global_config() -> bool:
    """删全局记忆参数（回退代码默认值）；返回是否删除成功。"""
    return await config_delete(GLOBAL_CONFIG_KEY)


__all__ = [
    "get_memory_global_config",
    "upsert_memory_global_config",
    "delete_memory_global_config",
]
