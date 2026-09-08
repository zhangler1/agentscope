# -*- coding: utf-8 -*-
"""运行时配置（RuntimeConfigs）通用读写层 —— PostgreSQL 持久化真源。

把**应用级、全局单份、运行时可变**的配置段统一存到 PG 的 ``runtime_configs`` 表
（``config_key`` / ``payload`` / ``updated_at``），提供按 ``config_key`` 的通用增删改查，
不绑定任何具体配置段。``summarization`` 等配置段通过本层按不同 ``config_key`` 存取，
新增配置段只需新增一个 ``config_key``，不改表结构。

存储模式与 ``system_prompt.py`` / ``memory_config.py`` 一致：懒加载独立 async
engine + 幂等建表 + 纯 ``text`` SQL，绕过框架表管理与 Alembic 迁移。payload 以
JSON 字符串落库 **TEXT 列**（代码层 ``json.dumps/loads`` 序列化；不用原生 JSON
类型以兼容 OceanBase 3.x / MySQL / PG / SQLite 全场景）。

用法::

    from bocomadp.runtime_config_store import config_get, config_set

    payload = await config_get("summarization")       # dict | None
    await config_set("summarization", {...})          # UPSERT
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.config import get_app_config

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# 通用配置表：key -> payload(JSON)。
_TABLE = "runtime_configs"

_CREATE_TABLE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    # ``key`` 是 MySQL 保留字，直接用会报 1064 语法错误；
    # 列名改 ``config_key`` 避免反引号/双引号的方言差异，MySQL/OB/PG/SQLite 全兼容。
    "config_key VARCHAR(255) PRIMARY KEY, "
    # payload 用 TEXT 而非 JSON：OceanBase 3.x 无原生 JSON 列类型，
    # 且代码层已手工 _encode/_decode 做 json.dumps/loads，
    # TEXT 兼容 MySQL/OB/PG/SQLite 全场景（OB 4.x 亦可用）。
    "payload TEXT NOT NULL, "
    "updated_at TIMESTAMP NOT NULL"
    ")"
)

_engine: Any = None
_engine_lock = asyncio.Lock()
# 按 engine 实例记录是否已建表：同一 engine 只建一次，换 engine（如测试替换）
# 则重新建表，保证在替换 engine 的测试场景下也能正确建表。
_initialized_for: Any = None


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。"""
    global _engine
    if _engine is None:
        async with _engine_lock:
            if _engine is None:
                _engine = create_async_engine(
                    get_app_config().db.url,
                    pool_pre_ping=True,
                )
    return _engine


async def _ensure_table() -> None:
    """按需幂等建表（``runtime_configs``）：每个 engine 只执行一次。"""
    global _initialized_for
    engine = await _get_engine()
    if _initialized_for is engine:
        return

    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))
    _initialized_for = engine


def _encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _decode(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None
    return None


async def config_get(key: str) -> dict | None:
    """读一行配置 payload；无记录或 DB 不可用返回 ``None``。"""
    try:
        await _ensure_table()
        engine = await _get_engine()

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        f"SELECT payload FROM {_TABLE} WHERE config_key = :key",
                    ),
                    {"key": key},
                )
            ).first()
        if row is None:
            return None
        return _decode(row[0])
    except Exception as e:  # pragma: no cover - DB 不可用
        logger.warning("runtime_config: config_get(%r) failed: %s", key, e)
        return None


async def config_set(key: str, payload: dict) -> None:
    """UPSERT 一行配置（key 不存在则新增，存在则覆盖）。"""
    await _ensure_table()
    engine = await _get_engine()

    # ON CONFLICT 是 PG/sqlite 方言；MySQL 用 ON DUPLICATE KEY UPDATE。
    if engine.dialect.name == "mysql":
        upsert = (
            "ON DUPLICATE KEY UPDATE "
            "payload = VALUES(payload), updated_at = VALUES(updated_at)"
        )
    else:
        upsert = (
            "ON CONFLICT (config_key) DO UPDATE SET "
            "payload = :payload, updated_at = :ts"
        )

    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} (config_key, payload, updated_at) "
                "VALUES (:key, :payload, :ts) " + upsert,
            ),
            {"key": key, "payload": _encode(payload), "ts": datetime.now()},
        )


async def config_delete(key: str) -> bool:
    """删除一行配置；返回是否删除成功（不存在返回 False）。"""
    await _ensure_table()
    engine = await _get_engine()

    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"DELETE FROM {_TABLE} WHERE config_key = :key"),
            {"key": key},
        )
    return result.rowcount > 0


async def config_list() -> dict[str, dict]:
    """列出全部配置段（key -> payload）。"""
    try:
        await _ensure_table()
        engine = await _get_engine()

        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(f"SELECT config_key, payload FROM {_TABLE}"))
            ).all()
        result: dict[str, dict] = {}
        for r in rows:
            payload = _decode(r[1])
            if payload is not None:
                result[str(r[0])] = payload
        return result
    except Exception as e:  # pragma: no cover - DB 不可用
        logger.warning("runtime_config: config_list failed: %s", e)
        return {}


async def get_typed_config(key: str, model_cls: type[_T]) -> _T | None:
    """读 ``key`` 并反序列化为指定 pydantic 模型；无记录返回 ``None``。"""
    payload = await config_get(key)
    if payload is None:
        return None
    try:
        return model_cls(**payload)
    except Exception as e:  # 反序列化失败视为无有效配置
        logger.warning(
            "runtime_config: config %r invalid for %s: %s",
            key,
            model_cls.__name__,
            e,
        )
        return None


__all__ = [
    "config_get",
    "config_set",
    "config_delete",
    "config_list",
    "get_typed_config",
]
