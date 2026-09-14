# -*- coding: utf-8 -*-
"""智能体凭证绑定表（``agent_credential``）—— 增删改查管理接口。

记录「某个智能体使用哪条凭证」的绑定关系（一条记录 = 一个绑定）：
一个 agent 对应一条凭证，凭证本身仍在框架 ``credentials`` 表中。

存储模式与 ``model_registry.py`` / ``runtime_config_store.py`` 一致：
懒加载独立 async engine（URL 取 ``get_app_config().db.url``，与框架
storage 同库、独立连接池）+ 幂等建表 + 纯 ``text`` SQL 绕过框架表管理
与 Alembic 迁移。

存储方言（跨库）：
    连接串决定实际后端，**PG / MySQL / OceanBase(MySQL 模式) 均可**：

    - 无 ``ON CONFLICT`` / ``RETURNING`` / ``::cast`` / JSONB 等方言语法
    - TEXT 列不带 DEFAULT（MySQL 不允许 TEXT/BLOB/JSON 有默认值）
    - TIMESTAMP 显式写 ``DEFAULT CURRENT_TIMESTAMP``（避免 MySQL 给首个
      TIMESTAMP 列隐式补 ``ON UPDATE CURRENT_TIMESTAMP``）

表字段：
    agent_id        智能体 ID（主键）
    credential_id   绑定的凭证 ID
    created_at      创建时间
    updated_at      更新时间

接口：

- ``GET    /agent-credential``                    列出全部绑定（可按凭证过滤）
- ``GET    /agent-credential/{agent_id}``         查询单个智能体的绑定
- ``POST   /agent-credential``                    新增绑定（agent_id 已存在 → 409）
- ``PUT    /agent-credential/{agent_id}``         修改绑定（换凭证；不存在 → 404）
- ``DELETE /agent-credential/{agent_id}``         删除绑定（不存在 → 404）
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

logger = logging.getLogger(__name__)

agent_credential_router = APIRouter(
    prefix="/agent-credential",
    tags=["agent-credential"],
)


# ---------------------------------------------------------------------------
# 常量与建表
# ---------------------------------------------------------------------------

_TABLE = "agent_credential"

_COLUMNS = "agent_id, credential_id, created_at, updated_at"

_CREATE_TABLE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
    "agent_id VARCHAR(255) PRIMARY KEY, "
    "credential_id VARCHAR(255) NOT NULL, "
    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ")"
)


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
    """幂等建表（``agent_credential``）。"""
    assert _engine is not None
    from sqlalchemy import text

    async with _engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))


async def _fetch_one(agent_id: str) -> dict[str, Any] | None:
    """按 agent_id 读一行；不存在返回 ``None``。"""
    from sqlalchemy import text

    engine = await _get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                f"SELECT {_COLUMNS} FROM {_TABLE} "
                "WHERE agent_id = :agent_id",
            ),
            {"agent_id": agent_id},
        )
        row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class AgentCredentialItem(BaseModel):
    """绑定记录（查询响应）。"""

    agent_id: str = Field(description="智能体 ID（主键）")
    credential_id: str = Field(description="绑定的凭证 ID")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")


class AgentCredentialCreateRequest(BaseModel):
    """新增绑定请求。"""

    agent_id: str = Field(description="智能体 ID（唯一）")
    credential_id: str = Field(description="要绑定的凭证 ID")


class AgentCredentialUpdateRequest(BaseModel):
    """修改绑定请求（换绑凭证）。"""

    credential_id: str = Field(description="新的凭证 ID")


def _row_to_item(row: dict[str, Any]) -> AgentCredentialItem:
    """数据库行 → 响应模型。"""
    return AgentCredentialItem(
        agent_id=row["agent_id"],
        credential_id=row["credential_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@agent_credential_router.get(
    "",
    response_model=list[AgentCredentialItem],
    summary="列出全部智能体凭证绑定",
)
async def list_agent_credentials(
    credential_id: str | None = Query(
        default=None,
        description="按凭证 ID 过滤（精确匹配）；不传返回全部",
    ),
    user_id: str = Depends(get_current_user_id),
) -> list[AgentCredentialItem]:
    """列出全部绑定，可按 ``credential_id`` 过滤。"""
    from sqlalchemy import text

    engine = await _get_engine()
    sql = f"SELECT {_COLUMNS} FROM {_TABLE}"
    params: dict[str, Any] = {}
    if credential_id:
        sql += " WHERE credential_id = :credential_id"
        params["credential_id"] = credential_id
    sql += " ORDER BY agent_id"

    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [_row_to_item(dict(r)) for r in rows]


@agent_credential_router.get(
    "/{agent_id}",
    response_model=AgentCredentialItem,
    summary="查询单个智能体的凭证绑定",
)
async def get_agent_credential(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> AgentCredentialItem:
    """按 agent_id 查询；不存在 → 404。"""
    row = await _fetch_one(agent_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent credential binding for {agent_id!r} not found.",
        )
    return _row_to_item(row)


@agent_credential_router.post(
    "",
    response_model=AgentCredentialItem,
    status_code=status.HTTP_201_CREATED,
    summary="新增智能体凭证绑定",
)
async def create_agent_credential(
    body: AgentCredentialCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> AgentCredentialItem:
    """新增绑定；``agent_id`` 已存在 → 409。"""
    from sqlalchemy import text

    if await _fetch_one(body.agent_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Binding for agent {body.agent_id!r} already exists.",
        )

    engine = await _get_engine()
    now = datetime.now()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {_TABLE} ({_COLUMNS}) VALUES ("
                ":agent_id, :credential_id, :created_at, :updated_at"
                ")",
            ),
            {
                "agent_id": body.agent_id,
                "credential_id": body.credential_id,
                "created_at": now,
                "updated_at": now,
            },
        )

    logger.info(
        "agent_credential: created agent=%s credential=%s (user=%s)",
        body.agent_id,
        body.credential_id,
        user_id,
    )
    row = await _fetch_one(body.agent_id)
    assert row is not None
    return _row_to_item(row)


@agent_credential_router.put(
    "/{agent_id}",
    response_model=AgentCredentialItem,
    summary="修改智能体凭证绑定",
)
async def update_agent_credential(
    agent_id: str,
    body: AgentCredentialUpdateRequest,
    user_id: str = Depends(get_current_user_id),
) -> AgentCredentialItem:
    """换绑凭证；``agent_id`` 不存在 → 404。"""
    from sqlalchemy import text

    row = await _fetch_one(agent_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent credential binding for {agent_id!r} not found.",
        )

    engine = await _get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE {_TABLE} SET credential_id = :credential_id, "
                "updated_at = :updated_at WHERE agent_id = :agent_id",
            ),
            {
                "agent_id": agent_id,
                "credential_id": body.credential_id,
                "updated_at": datetime.now(),
            },
        )

    logger.info(
        "agent_credential: updated agent=%s credential=%s (user=%s)",
        agent_id,
        body.credential_id,
        user_id,
    )
    updated = await _fetch_one(agent_id)
    assert updated is not None
    return _row_to_item(updated)


@agent_credential_router.delete(
    "/{agent_id}",
    summary="删除智能体凭证绑定",
)
async def delete_agent_credential(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """删除绑定；不存在 → 404。"""
    from sqlalchemy import text

    engine = await _get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"DELETE FROM {_TABLE} WHERE agent_id = :agent_id"),
            {"agent_id": agent_id},
        )
    if not result.rowcount:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent credential binding for {agent_id!r} not found.",
        )

    logger.info(
        "agent_credential: deleted agent=%s (user=%s)",
        agent_id,
        user_id,
    )
    return {"deleted": True, "agent_id": agent_id}


__all__ = ["agent_credential_router"]
