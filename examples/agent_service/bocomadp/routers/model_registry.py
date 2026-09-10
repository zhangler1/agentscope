# -*- coding: utf-8 -*-
"""模型注册表（``model_registry``）—— PG 持久化 + 增删改查管理接口。

记录「现有可用模型」的元信息，供前端/管理层维护。存储模式与
``system_prompt.py`` / ``runtime_config_store.py`` 一致：懒加载独立 async
engine + 幂等建表 + 纯 ``text`` SQL，绕过框架表管理与 Alembic 迁移。

本表是**独立的模型台账**，与 Redis ``bocomadp:model:think_tag``（ELLM
运行时模型候选，由 ``/ellm-models`` 维护）**不同源、不做同步**——
两者用途不同：本表供资产/能力登记与查询，Redis 那一份只服务
``EllmChatModel.list_models()`` 的热路径。

存储方言：
    连接串取 ``get_app_config().db.url``，**PG / MySQL / OceanBase(MySQL
    模式) 均可**——DDL 与 DML 均为跨方言写法（无 ``ON CONFLICT``、
    ``RETURNING``、``::cast``、JSONB 等方言语法）；以下差异已特别处理：

    - TEXT 列不带 DEFAULT（MySQL 不允许 TEXT/BLOB/JSON 有默认值）
    - TIMESTAMP 列显式写 DEFAULT CURRENT_TIMESTAMP（避免 MySQL 给首个
      TIMESTAMP 列隐式补 ``ON UPDATE CURRENT_TIMESTAMP``）
    - 列名避开 MySQL 保留字（如用 ``remark`` 而非 ``desc``/``key``）

表字段：
    model_name                 模型名称（主键）
    model_type                 模型类型：LLM / VLM / LLM,VLM（两者兼具）
    context_size               上下文长度（token）
    is_reasoning               是否推理模型
    thinking_default           是否默认开启思维链
    thinking_switchable        是否支持思维链切换
    supports_response_format   是否支持 response_format
    vendor                     模型厂商
    remark                     模型备注
    created_at / updated_at    审计时间戳

接口：

- ``GET    /model-registry``              列出模型（可按 model_type / vendor 过滤）
- ``GET    /model-registry/{model_name}`` 查询单个模型
- ``POST   /model-registry``              新增模型（已存在 → 409）
- ``PUT    /model-registry/{model_name}`` 修改模型（支持改名）
- ``DELETE /model-registry/{model_name}`` 删除模型（不存在 → 404）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from agentscope.app.deps import get_current_user_id

logger = logging.getLogger(__name__)

model_registry_router = APIRouter(
    prefix="/model-registry",
    tags=["model-registry"],
)


# ---------------------------------------------------------------------------
# 常量与建表
# ---------------------------------------------------------------------------

_TABLE = "model_registry"

#: 单个模型类型取值。
MODEL_TYPES = ("LLM", "VLM")

#: ``model_type`` 合法取值：单类型，或两者兼具（逗号分隔，固定 LLM 在前）。
MODEL_TYPE_VALUES = ("LLM", "VLM", "LLM,VLM")

_COLUMNS = (
    "model_name, model_type, context_size, output_size, think_tag, "
    "is_reasoning, thinking_default, thinking_switchable, "
    "supports_response_format, vendor, remark, created_at, updated_at"
)

#: 跨方言 DDL（PG / MySQL / OceanBase-MySQL 均可执行）：
#: - ``remark`` 用 TEXT 且**不带 DEFAULT**：MySQL 不允许 TEXT/BLOB/JSON
#:   列有默认值（否则 ERROR 1101），调用方 INSERT 始终显式传值即可。
#: - 两个 TIMESTAMP 都显式写 ``DEFAULT CURRENT_TIMESTAMP``：MySQL 在
#:   ``explicit_defaults_for_timestamp=OFF`` 时会给**第一个** TIMESTAMP 列
#:   隐式补 ``ON UPDATE CURRENT_TIMESTAMP``，导致 UPDATE 时 created_at
#:   被连带改写；显式声明可规避（且不带 ON UPDATE）。
_CREATE_TABLE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "model_name VARCHAR(255) PRIMARY KEY, "
    "model_type VARCHAR(16) NOT NULL, "
    "context_size INTEGER NOT NULL, "
    "output_size INTEGER NOT NULL DEFAULT 384000, "
    "think_tag BOOLEAN NOT NULL DEFAULT FALSE, "
    "is_reasoning BOOLEAN NOT NULL DEFAULT FALSE, "
    "thinking_default BOOLEAN NOT NULL DEFAULT FALSE, "
    "thinking_switchable BOOLEAN NOT NULL DEFAULT FALSE, "
    "supports_response_format BOOLEAN NOT NULL DEFAULT FALSE, "
    "vendor VARCHAR(128) NOT NULL DEFAULT '', "
    "remark TEXT NOT NULL, "
    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ")"
)

#: 存量表补列（``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列）。
#: 这里存 ``列名 -> 列定义``，由 :func:`_ensure_table` 先用 inspector
#: 探测实际列再决定是否 ALTER——**不再依赖吞异常**，PG / MySQL / OB 通用
#: （SQLAlchemy ``inspect().get_columns()`` 已做方言适配）。
_MIGRATE_COLUMNS: dict[str, str] = {
    "output_size": "INTEGER NOT NULL DEFAULT 384000",
    "think_tag": "BOOLEAN NOT NULL DEFAULT FALSE",
}


# ---------------------------------------------------------------------------
# 存储层（懒加载 engine + 幂等建表）
# ---------------------------------------------------------------------------

_engine: Any = None
_engine_lock = asyncio.Lock()


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。"""
    global _engine
    if _engine is None:
        async with _engine_lock:
            if _engine is None:
                from sqlalchemy.ext.asyncio import create_async_engine

                from bocomadp.config import get_app_config

                _engine = create_async_engine(
                    get_app_config().db.url,
                    pool_pre_ping=True,
                )
                await _ensure_table()
    return _engine


async def _ensure_table() -> None:
    """幂等建表（``model_registry``），并为存量表补齐新增列。

    跨方言要点：

    - MySQL 的 DDL 会**隐式提交**，不能依赖“异常时回滚同事务”那套 PG
      语义；因此补列前先用 inspector 探测列是否已存在，避免发出会被数据库
      拒绝的 ALTER。
    - MySQL 的 ``information_schema`` 里 ``table_name`` 大小写敏感取决于
      ``lower_case_table_names``，SQLAlchemy inspector 已按方言处理，这里
      直接用表名常量即可。
    """
    assert _engine is not None
    from sqlalchemy import inspect, text

    async with _engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))

    def _missing_columns(sync_conn: Any) -> list[str]:
        existing = {c["name"] for c in inspect(sync_conn).get_columns(_TABLE)}
        return [name for name in _MIGRATE_COLUMNS if name not in existing]

    async with _engine.connect() as conn:
        missing = await conn.run_sync(_missing_columns)

    for name in missing:
        ddl = f"ALTER TABLE {_TABLE} ADD COLUMN {name} {_MIGRATE_COLUMNS[name]}"
        try:
            async with _engine.begin() as conn:
                await conn.execute(text(ddl))
            logger.info("model_registry: added column %s", name)
        except Exception as e:  # noqa: BLE001 —— 并发下重复补列等场景
            logger.warning(
                "model_registry: add column %s skipped (%s)", name, e,
            )


async def _fetch_one(model_name: str) -> dict[str, Any] | None:
    """按模型名读一行；不存在返回 ``None``。"""
    from sqlalchemy import text

    engine = await _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM {_TABLE} "
                "WHERE model_name = :model_name",
            ),
            {"model_name": model_name},
        )
        row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _normalize_model_types(value: str) -> str:
    """归一化模型类型串为 ``LLM`` / ``VLM`` / ``LLM,VLM`` 之一。

    接受任意常见分隔符（逗号、顿号、斜杠、加号、竖线、&、空格）与
    ``all`` / ``both`` 别名，大小写不敏感，去重后按 :data:`MODEL_TYPES`
    顺序输出，例如 ``"vlm, llm"`` → ``"LLM,VLM"``。

    含非法 token 或空串时原样返回，由 pydantic 校验器抛 422。
    """
    import re

    tokens = [
        t.strip().upper()
        for t in re.split(r"[,，、/|+&\s]+", value or "")
        if t.strip()
    ]
    expanded: list[str] = []
    for token in tokens:
        if token in ("ALL", "BOTH"):
            expanded.extend(MODEL_TYPES)
        else:
            expanded.append(token)

    ordered = [t for t in MODEL_TYPES if t in set(expanded)]
    if len(ordered) != len(set(expanded)):
        # 存在非法 token（或重复之外的情况）——交校验器报错
        return (value or "").strip()
    return ",".join(ordered)


class ModelRegistryItem(BaseModel):
    """模型注册表条目（查询响应）。"""

    model_name: str = Field(description="模型名称（主键）")
    model_type: str = Field(description="模型类型：LLM / VLM")
    context_size: int = Field(description="上下文长度（token）")
    output_size: int = Field(description="最大输出 token 数")
    think_tag: bool = Field(
        description="是否注入 <think> 标签（与 Redis think_tag 同义）",
    )
    is_reasoning: bool = Field(description="是否推理模型")
    thinking_default: bool = Field(description="是否默认开启思维链")
    thinking_switchable: bool = Field(description="是否支持思维链切换")
    supports_response_format: bool = Field(
        description="是否支持 response_format",
    )
    vendor: str = Field(description="模型厂商")
    remark: str = Field(description="模型备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")


class ModelRegistryCreateRequest(BaseModel):
    """新增模型请求。"""

    model_name: str = Field(description="模型名称（唯一）")
    model_type: str = Field(
        default="LLM",
        description="模型类型：LLM / VLM / LLM,VLM（两者兼具）",
    )
    context_size: int = Field(gt=0, description="上下文长度（token）")
    output_size: int = Field(
        default=384000,
        gt=0,
        description="最大输出 token 数",
    )
    think_tag: bool = Field(
        default=False,
        description="是否注入 <think> 标签（与 Redis think_tag 同义）",
    )
    is_reasoning: bool = Field(default=False, description="是否推理模型")
    thinking_default: bool = Field(
        default=False,
        description="是否默认开启思维链",
    )
    thinking_switchable: bool = Field(
        default=False,
        description="是否支持思维链切换",
    )
    supports_response_format: bool = Field(
        default=False,
        description="是否支持 response_format",
    )
    vendor: str = Field(default="", description="模型厂商")
    remark: str = Field(default="", description="模型备注")

    @field_validator("model_type")
    @classmethod
    def _check_model_type(cls, v: str) -> str:
        normalized = _normalize_model_types(v)
        if normalized not in MODEL_TYPE_VALUES:
            raise ValueError(
                f"model_type must be one of {list(MODEL_TYPE_VALUES)}",
            )
        return normalized


class ModelRegistryUpdateRequest(BaseModel):
    """修改模型请求（全字段可选，缺省保持原值）。"""

    model_name: str | None = Field(
        default=None,
        description="新模型名（改名用；缺省保持原名）",
    )
    model_type: str | None = Field(
        default=None,
        description="模型类型：LLM / VLM / LLM,VLM（两者兼具）",
    )
    context_size: int | None = Field(
        default=None,
        gt=0,
        description="上下文长度（token）",
    )
    output_size: int | None = Field(
        default=None,
        gt=0,
        description="最大输出 token 数",
    )
    think_tag: bool | None = Field(
        default=None,
        description="是否注入 <think> 标签（与 Redis think_tag 同义）",
    )
    is_reasoning: bool | None = Field(default=None, description="是否推理模型")
    thinking_default: bool | None = Field(
        default=None,
        description="是否默认开启思维链",
    )
    thinking_switchable: bool | None = Field(
        default=None,
        description="是否支持思维链切换",
    )
    supports_response_format: bool | None = Field(
        default=None,
        description="是否支持 response_format",
    )
    vendor: str | None = Field(default=None, description="模型厂商")
    remark: str | None = Field(default=None, description="模型备注")

    @field_validator("model_type")
    @classmethod
    def _check_model_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = _normalize_model_types(v)
        if normalized not in MODEL_TYPE_VALUES:
            raise ValueError(
                f"model_type must be one of {list(MODEL_TYPE_VALUES)}",
            )
        return normalized


def _row_to_item(row: dict[str, Any]) -> ModelRegistryItem:
    """数据库行 → 响应模型。"""
    return ModelRegistryItem(
        model_name=row["model_name"],
        model_type=row["model_type"],
        context_size=int(row["context_size"]),
        output_size=int(row["output_size"]),
        think_tag=bool(row["think_tag"]),
        is_reasoning=bool(row["is_reasoning"]),
        thinking_default=bool(row["thinking_default"]),
        thinking_switchable=bool(row["thinking_switchable"]),
        supports_response_format=bool(row["supports_response_format"]),
        vendor=row["vendor"] or "",
        remark=row["remark"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@model_registry_router.get(
    "",
    response_model=list[ModelRegistryItem],
    summary="列出模型",
)
async def list_models(
    model_type: str | None = Query(
        default=None,
        description=(
            "按模型类型过滤（LLM / VLM / LLM,VLM）；传 LLM 时"
            "「两者兼具」的模型同样命中"
        ),
    ),
    vendor: str | None = Query(
        default=None,
        description="按模型厂商过滤（精确匹配）",
    ),
    user_id: str = Depends(get_current_user_id),
) -> list[ModelRegistryItem]:
    """列出全部模型，可按 ``model_type`` / ``vendor`` 过滤。"""
    from sqlalchemy import text

    engine = await _get_engine()
    where: list[str] = []
    params: dict[str, Any] = {}
    if model_type:
        # 成员匹配：按 ``LLM`` 过滤时，``LLM,VLM``（两者兼具）也要命中，
        # 故用 LIKE 包含判断而非等值比较。
        where.append("model_type LIKE :model_type")
        params["model_type"] = f"%{_normalize_model_types(model_type)}%"
    if vendor:
        where.append("vendor = :vendor")
        params["vendor"] = vendor
    sql = f"SELECT {_COLUMNS} FROM {_TABLE}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY model_name"

    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [_row_to_item(dict(r)) for r in rows]


@model_registry_router.get(
    "/{model_name}",
    response_model=ModelRegistryItem,
    summary="查询单个模型",
)
async def get_model(
    model_name: str,
    user_id: str = Depends(get_current_user_id),
) -> ModelRegistryItem:
    """按模型名查询；不存在 → 404。"""
    row = await _fetch_one(model_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found.",
        )
    return _row_to_item(row)


@model_registry_router.post(
    "",
    response_model=ModelRegistryItem,
    status_code=status.HTTP_201_CREATED,
    summary="新增模型",
)
async def create_model(
    body: ModelRegistryCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> ModelRegistryItem:
    """新增模型；模型名已存在 → 409。"""
    from sqlalchemy import text

    if await _fetch_one(body.model_name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model {body.model_name!r} already exists.",
        )

    engine = await _get_engine()
    now = datetime.now()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES ("
                ":model_name, :model_type, :context_size, :output_size, "
                ":think_tag, :is_reasoning, "
                ":thinking_default, :thinking_switchable, "
                ":supports_response_format, :vendor, :remark, "
                ":created_at, :updated_at"
                ")",
            ),
            {
                "model_name": body.model_name,
                "model_type": body.model_type,
                "context_size": body.context_size,
                "output_size": body.output_size,
                "think_tag": body.think_tag,
                "is_reasoning": body.is_reasoning,
                "thinking_default": body.thinking_default,
                "thinking_switchable": body.thinking_switchable,
                "supports_response_format": body.supports_response_format,
                "vendor": body.vendor,
                "remark": body.remark,
                "created_at": now,
                "updated_at": now,
            },
        )

    logger.info(
        "model_registry: created model=%s type=%s vendor=%s (user=%s)",
        body.model_name,
        body.model_type,
        body.vendor,
        user_id,
    )
    row = await _fetch_one(body.model_name)
    assert row is not None
    return _row_to_item(row)


@model_registry_router.put(
    "/{model_name}",
    response_model=ModelRegistryItem,
    summary="修改模型",
)
async def update_model(
    model_name: str,
    body: ModelRegistryUpdateRequest,
    user_id: str = Depends(get_current_user_id),
) -> ModelRegistryItem:
    """修改模型字段（缺省保持原值），支持改名；不存在 → 404。"""
    from sqlalchemy import text

    row = await _fetch_one(model_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found.",
        )

    target = body.model_name or model_name
    if target != model_name and await _fetch_one(target) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model {target!r} already exists.",
        )

    assignments: dict[str, Any] = {
        "model_name": target,
        "model_type": (
            body.model_type
            if body.model_type is not None
            else row["model_type"]
        ),
        "context_size": (
            body.context_size
            if body.context_size is not None
            else row["context_size"]
        ),
        "output_size": (
            body.output_size
            if body.output_size is not None
            else row["output_size"]
        ),
        "think_tag": (
            body.think_tag
            if body.think_tag is not None
            else row["think_tag"]
        ),
        "is_reasoning": (
            body.is_reasoning
            if body.is_reasoning is not None
            else row["is_reasoning"]
        ),
        "thinking_default": (
            body.thinking_default
            if body.thinking_default is not None
            else row["thinking_default"]
        ),
        "thinking_switchable": (
            body.thinking_switchable
            if body.thinking_switchable is not None
            else row["thinking_switchable"]
        ),
        "supports_response_format": (
            body.supports_response_format
            if body.supports_response_format is not None
            else row["supports_response_format"]
        ),
        "vendor": body.vendor if body.vendor is not None else row["vendor"],
        "remark": body.remark if body.remark is not None else row["remark"],
        "updated_at": datetime.now(),
        "old_model_name": model_name,
    }
    set_clause = ", ".join(
        f"{col} = :{col}" for col in assignments if col != "old_model_name"
    )

    engine = await _get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE {_TABLE} SET {set_clause} "
                "WHERE model_name = :old_model_name",
            ),
            assignments,
        )

    logger.info(
        "model_registry: updated model=%s->%s (user=%s)",
        model_name,
        target,
        user_id,
    )
    updated = await _fetch_one(target)
    assert updated is not None
    return _row_to_item(updated)


@model_registry_router.delete(
    "/{model_name}",
    summary="删除模型",
)
async def delete_model(
    model_name: str,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """删除模型；不存在 → 404。"""
    from sqlalchemy import text

    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"DELETE FROM {_TABLE} WHERE model_name = :model_name"),
            {"model_name": model_name},
        )
    if not result.rowcount:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_name!r} not found.",
        )

    logger.info(
        "model_registry: deleted model=%s (user=%s)",
        model_name,
        user_id,
    )
    return {"deleted": True, "model_name": model_name}


__all__ = ["model_registry_router"]
