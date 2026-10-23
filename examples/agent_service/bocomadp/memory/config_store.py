# -*- coding: utf-8 -*-
"""记忆配置薄封装（agent 级）：模块级 ``_memory_storage`` 引用，走存储端口。

真源 = :class:`memory.storage.MemoryStorage` 的 agent 配置四方法；本层只做
「未装配 → 兜底」与 sweeper 的启用过滤视图。装配入口
:func:`memory.setup_memory`（``__init__.py``）写入
下方三个模块级引用。

facade 与端口方法一一对应（``get_memory_config`` / ``upsert_memory_config``
/ ``delete_memory_config``），读侧宽松（未装配 → None / False）、写侧
fail-fast（未装配 → RuntimeError）。
"""
from __future__ import annotations

from typing import Any

from .storage import MemoryStorage
from .storage._models import MemoryAgentConfigRecord

# 模块级私有引用：setup_memory 装入
_memory_storage: MemoryStorage | None = None

# 框架 StorageBase 引用（公开方法 get_agent 供 routers 越权校验）。
_storage: Any = None

# 框架 MessageBus 引用（中间件 is_locked 预检 / 提取锁 / sweeper）。
_message_bus: Any = None


def get_memory_storage() -> MemoryStorage | None:
    """取记忆存储端口；未装配 → None（读侧宽松）。

    供其它模块取记忆后端，免于直接访问模块级私有变量。
    """
    return _memory_storage


async def get_memory_config(agent_id: str) -> MemoryAgentConfigRecord | None:
    """读 agent 配置；未装配 / 无记录 → None（读侧宽松）。"""
    if _memory_storage is None:
        return None
    return await _memory_storage.get_memory_config(agent_id)


async def upsert_memory_config(
    record: MemoryAgentConfigRecord,
) -> MemoryAgentConfigRecord:
    """写 agent 配置；未装配 → RuntimeError（写侧 fail-fast）。"""
    if _memory_storage is None:
        raise RuntimeError("setup_memory 未调用（无 storage）")
    return await _memory_storage.upsert_memory_config(record)


async def delete_memory_config(agent_id: str) -> bool:
    """删 agent 配置；未装配 → False。"""
    if _memory_storage is None:
        return False
    return await _memory_storage.delete_memory_config(agent_id)


#: 列表分页大小：存储端按页返回（默认 LIMIT 100，避免无条件全表扫描）
_LIST_PAGE_SIZE = 100


async def list_extraction_enabled() -> list[
    tuple[str, MemoryAgentConfigRecord]
]:
    """提取启用条目 ``(agent_id, record)``。

    返回 ``(agent_id, record)`` 元组（sweeper 免二次解包）；只回
    ``extraction_enabled=True`` 条目（sweeper 静默补提取的候选名单）。
    端口按 ``_LIST_PAGE_SIZE`` 分页，这里循环取全量——不因单页上限漏掉
    超出页宽的 agent。
    """
    if _memory_storage is None:
        return []
    enabled: list[tuple[str, MemoryAgentConfigRecord]] = []
    offset = 0
    while True:
        page = await _memory_storage.list_memory_configs(
            limit=_LIST_PAGE_SIZE, offset=offset
        )
        enabled.extend((r.agent_id, r) for r in page if r.extraction_enabled)
        if len(page) < _LIST_PAGE_SIZE:
            return enabled
        offset += _LIST_PAGE_SIZE


__all__ = [
    "get_memory_config",
    "upsert_memory_config",
    "delete_memory_config",
    "get_memory_storage",
    "list_extraction_enabled",
]
