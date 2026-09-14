# -*- coding: utf-8 -*-
"""MemoryConfig 模型 + ``agent_memory_configs``（payload TEXT 全量存储）存取层。

表结构：``agent_id``(PK) / ``user_id`` / ``payload``(TEXT) / ``updated_at``；
payload **全量落库**（``cfg.model_dump()`` 后 JSON 化，含默认值字段，方便
直接查库核对），读取时反序列化为 MemoryConfig；``extra=allow`` 支持动态新增字段。

与旧 ``bocomadp/memory_config.py``（4 固定列、无 payload）不同：新模型字段可
扩展（top_k/agent_plat/...），旧实现已被本模块取代（见 Task 7）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.config import get_app_config

# 主键 agent_id 全局唯一，user_id 仅作归属标记（与框架 agents 表
# user_id 归属 + agent_id 模型对齐）；updated_at 用 TIMESTAMP。
# payload 用 TEXT 而非 JSON/JSONB：MySQL/OceanBase 3.x 无原生 JSONB 列
# （PG 兼容亦可用），且代码层已手工 json.dumps/loads 序列化，
# TEXT 兼容 MySQL/OB/PG/SQLite 全场景（与 runtime_config_store 同模式）。
# 读取时兼容 dict（原生 JSON 列）/ str（TEXT 列）两种形态。
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS agent_memory_configs ("
    "agent_id VARCHAR(255) PRIMARY KEY, "
    "user_id VARCHAR(255) NOT NULL, "
    "payload TEXT NOT NULL, "
    "updated_at TIMESTAMP NOT NULL)"
)


class MemoryConfig(BaseModel):
    """智能体记忆配置；入库 payload 为全量字段（含默认值），extra=allow 支持动态新增。

    字段命名本地 snake_case；发送平台时在边界映射为 camelCase（见 platform.py）。
    """

    model_config = ConfigDict(extra="allow")

    memory_enabled: bool = Field(
        default=False,
        description="记忆开关。",
    )
    memory_type: int = Field(
        default=0,
        ge=0,
        le=1,
        description="记忆类型：0=程序性记忆，1=事务性记忆。",
    )
    update_rounds: int = Field(
        default=10,
        ge=1,
        description="每 N 轮对话触发一次提取（>=1，禁止纯静默）。",
    )
    memory_prompt: str = Field(
        default="",
        description="记忆更新提示词（空串走平台默认）。",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        description="每轮检索注入的记忆条数上限。",
    )
    agent_plat: int = Field(
        default=0,
        ge=0,
        le=2,
        description="智能体平台标识（平台契约 agentPlat）。",
    )
    message_type: int = Field(
        default=0,
        ge=0,
        le=1,
        description="消息类型（平台契约 messageType）。",
    )
    infer_flag: str = Field(
        default="1",
        description="推理标识（平台契约 inferFlag）。",
    )
    caller: str = Field(
        default="",
        description="平台注册返回的调用方标识（非空即已注册，避免重复注册）。",
    )


_engine: Any = None
# 按 engine 实例记录是否已建表：同一 engine 只建一次表，换 engine（如测试
# 替换）则重新建表，保证在替换 engine 的测试场景下也能正确建表。
_initialized_for: Any = None


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。"""
    global _engine
    if _engine is None:
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
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))
    _initialized_for = engine


async def memory_upsert(
    user_id: str,
    agent_id: str,
    cfg: MemoryConfig,
) -> None:
    """UPSERT 一条记忆配置：payload 全量替换为 ``cfg`` 全字段（含默认值）。"""
    await _ensure_table()
    engine = await _get_engine()
    payload = json.dumps(cfg.model_dump(), ensure_ascii=False)
    ts = datetime.now()
    # ON CONFLICT 是 PG/sqlite 方言；MySQL 用 ON DUPLICATE KEY UPDATE
    # （VALUES() 语法在 MySQL 5.7/8.0 与 MariaDB 均可用）。
    if engine.dialect.name == "mysql":
        upsert_sql = (
            "INSERT INTO agent_memory_configs "
            "(agent_id, user_id, payload, updated_at) "
            "VALUES (:agent_id, :user_id, :payload, :ts) "
            "ON DUPLICATE KEY UPDATE "
            "user_id = VALUES(user_id), "
            "payload = VALUES(payload), "
            "updated_at = VALUES(updated_at)"
        )
    else:
        upsert_sql = (
            "INSERT INTO agent_memory_configs "
            "(agent_id, user_id, payload, updated_at) "
            "VALUES (:agent_id, :user_id, :payload, :ts) "
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "user_id = EXCLUDED.user_id, "
            "payload = EXCLUDED.payload, "
            "updated_at = EXCLUDED.updated_at"
        )
    async with engine.begin() as conn:
        await conn.execute(
            text(upsert_sql),
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "payload": payload,
                "ts": ts,
            },
        )


async def memory_get(agent_id: str) -> MemoryConfig | None:
    """读取某 agent 的记忆配置（agent 级、按 agent_id 定位）；无记录返回 None。"""
    await _ensure_table()
    engine = await _get_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT payload FROM agent_memory_configs "
                    "WHERE agent_id = :agent_id",
                ),
                {"agent_id": agent_id},
            )
        ).mappings().first()
    if row is None:
        return None
    payload = row["payload"]
    # 原生 JSON 列返回 dict；TEXT 列返回 JSON 字符串（本项目全场景 TEXT）。
    data = payload if isinstance(payload, dict) else json.loads(payload)
    return MemoryConfig(**data)


async def memory_delete(agent_id: str) -> bool:
    """删除某 agent 的记忆配置（按 agent_id 定位）；返回是否删除成功。"""
    await _ensure_table()
    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "DELETE FROM agent_memory_configs "
                "WHERE agent_id = :agent_id",
            ),
            {"agent_id": agent_id},
        )
    return result.rowcount > 0


async def memory_list_enabled() -> list[tuple[str, MemoryConfig]]:
    """列出全部开启记忆的 (agent_id, config)，供静默扫描器枚举候选。

    配置是 agent 级：user_id 仅为归属标记、不参与本查询，杜绝调用方误把它
    当作会话 owner（spec 3.3/5.3）。扫描器从 active_sessions 反查真实 owner，
    故此处不再返回 user_id。配置量小，整表读取后按模型过滤即可，不依赖各
    数据库 JSON 过滤方言。
    """
    await _ensure_table()
    engine = await _get_engine()
    result: list[tuple[str, MemoryConfig]] = []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT agent_id, payload FROM agent_memory_configs"),
            )
        ).mappings().all()
    for row in rows:
        payload = row["payload"]
        data = payload if isinstance(payload, dict) else json.loads(payload)
        cfg = MemoryConfig(**data)
        if cfg.memory_enabled:
            result.append((str(row["agent_id"]), cfg))
    return result


__all__ = [
    "MemoryConfig",
    "memory_get",
    "memory_upsert",
    "memory_delete",
    "memory_list_enabled",
]
