# -*- coding: utf-8 -*-
"""Per-agent 记忆配置（4 记忆字段）存储：PG 表（仿 pool_config.py）。

写入路径（POST/PATCH /agent/ 包裹层）：PG UPSERT（持久化真源）。
读取路径（GET /agent/ 包裹层合并）：PG 查询，无记录返回 None，
调用方合并默认值。
记忆字段只被 API 读写，不在运行时热路径上，因此不做 Redis 热层
（区别于 pool_config 的沙箱分配热路径）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from bocomadp.config import get_app_config

# 主键 (user_id, agent_id) 与框架 agents 表的 (user_id 归属 + agent_id)
# 模型对齐，强化按用户隔离；updated_at 用 TIMESTAMP 与框架 agents 表
# （_sql/_tables.py 的 DateTime）一致；PG 端映射为 timestamp 类型。
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS agent_memory_configs ("
    "user_id VARCHAR(255) NOT NULL, "
    "agent_id VARCHAR(255) NOT NULL, "
    # VARCHAR(4000) 而非 TEXT：MySQL 严禁 TEXT/BLOB/JSON/GEOMETRY 列带
    # DEFAULT 值（错误码 1101），VARCHAR 则兼容 PG/MySQL 双库。
    # 4000 字符够装记忆提示词（system-hint 通常 < 2k）。
    "memory_update_prompt VARCHAR(4000) NOT NULL DEFAULT '', "
    "memory_enabled BOOLEAN NOT NULL DEFAULT FALSE, "
    "memory_type INTEGER NOT NULL DEFAULT 0, "
    "memory_update_rounds INTEGER NOT NULL DEFAULT 10, "
    "updated_at TIMESTAMP NOT NULL, "
    "PRIMARY KEY (user_id, agent_id)"
    ")"
)


class MemoryConfig(BaseModel):
    """智能体记忆配置（字段契约见设计文档 §4.1）。"""

    memory_update_prompt: str = Field(
        default="",
        description="记忆更新提示词；前端负责传默认值",
    )
    memory_enabled: bool = Field(
        default=False,
        description="记忆开关",
    )
    memory_type: int = Field(
        default=0,
        ge=0,
        le=1,
        description="记忆类型：0=程序性记忆，1=事务性记忆",
    )
    memory_update_rounds: int = Field(
        default=10,
        ge=0,
        description="记忆更新轮数：每 N 轮对话触发一次记忆更新；0=不按轮数触发",
    )


_engine: Any = None
# 按 engine 实例记录是否已建表：同一 engine 只建一次表，换 engine（如测试
# 替换）则重新建表，保证在替换 engine 的测试场景下也能正确建表。
_initialized_for: Any = None


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。"""
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        _engine = create_async_engine(
            get_app_config().db.url,
            pool_pre_ping=True,
        )
    return _engine


async def _ensure_table() -> None:
    """按需幂等建表（``agent_memory_configs``）：每个 engine 只执行一次。"""
    global _initialized_for
    engine = await _get_engine()
    if _initialized_for is engine:
        return
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))
    _initialized_for = engine


async def memory_upsert(
    user_id: str,
    agent_id: str,
    config: MemoryConfig,
) -> None:
    """原子 UPSERT 一条记忆配置（不存在则插入，存在则覆盖）。"""
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agent_memory_configs "
                "(user_id, agent_id, memory_update_prompt, memory_enabled, "
                " memory_type, memory_update_rounds, updated_at) "
                "VALUES (:user_id, :agent_id, :prompt, :enabled, "
                ":mtype, :rounds, :ts) "
                "ON DUPLICATE KEY UPDATE "
                "memory_update_prompt = :prompt, "
                "memory_enabled = :enabled, "
                "memory_type = :mtype, "
                "memory_update_rounds = :rounds, "
                "updated_at = :ts"
            ),
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "prompt": config.memory_update_prompt,
                "enabled": config.memory_enabled,
                "mtype": config.memory_type,
                "rounds": config.memory_update_rounds,
                "ts": datetime.now(),
            },
        )


async def memory_get(user_id: str, agent_id: str) -> MemoryConfig | None:
    """读取一条记忆配置；无记录返回 None（调用方合并默认值）。"""
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT memory_update_prompt, memory_enabled, "
                    "memory_type, memory_update_rounds "
                    "FROM agent_memory_configs "
                    "WHERE user_id = :user_id AND agent_id = :agent_id"
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )
        ).mappings().first()
    if row is None:
        return None
    return MemoryConfig(
        memory_update_prompt=row["memory_update_prompt"],
        memory_enabled=row["memory_enabled"],
        memory_type=row["memory_type"],
        memory_update_rounds=row["memory_update_rounds"],
    )


async def memory_delete(user_id: str, agent_id: str) -> bool:
    """删除一条记忆配置；返回是否删除成功（不存在返回 False）。"""
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "DELETE FROM agent_memory_configs "
                "WHERE user_id = :user_id AND agent_id = :agent_id",
            ),
            {"user_id": user_id, "agent_id": agent_id},
        )
    return result.rowcount > 0


__all__ = ["MemoryConfig", "memory_upsert", "memory_get", "memory_delete"]
