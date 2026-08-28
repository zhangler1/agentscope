# -*- coding: utf-8 -*-
"""系统提示词管理路由 —— 公共提示词的增删改查。

提供系统级公共提示词的 CRUD 接口，支持：
- 全局默认提示词（所有智能体共享）
- 按智能体自定义提示词（覆盖全局）

存储方式：PostgreSQL（``system_prompts`` 表，持久化真源）。
与 ``middleware/custom_prompt.py`` 读取端共用同一张表：
- 全局提示词: ``key = 'global'``
- 智能体提示词: ``key = {agent_id}``

存储模式与 ``pool_config.py`` / ``memory_config.py`` 一致：懒加载独立
async engine + 幂等建表 + text SQL。历史：旧版用 Redis（message_bus），
重启丢数据且与运行时层耦合；迁移到 PG 后重启不丢、多副本共享。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

logger = logging.getLogger(__name__)

system_prompt_router = APIRouter(
    prefix="/system-prompt",
    tags=["system-prompt"],
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class SystemPromptResponse(BaseModel):
    """系统提示词响应。"""
    key: str = Field(description="提示词标识（global 或 agent_id）")
    content: str = Field(description="提示词内容")
    is_default: bool = Field(description="是否为全局默认提示词")


class SystemPromptUpdateRequest(BaseModel):
    """更新系统提示词请求。"""
    content: str = Field(description="新的提示词内容")


class SystemPromptCreateRequest(BaseModel):
    """创建/覆盖系统提示词请求。"""
    content: str = Field(description="提示词内容")
    agent_id: str | None = Field(
        default=None,
        description="智能体 ID（为空则设置全局默认提示词）",
    )


# ---------------------------------------------------------------------------
# 存储层（PostgreSQL 真源）
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
    """幂等建表（``system_prompts``），与 custom_prompt.py 读取端同构。"""
    assert _engine is not None
    from sqlalchemy import text

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS system_prompts ("
                "key VARCHAR(255) PRIMARY KEY, "
                "content TEXT NOT NULL, "
                "updated_at TIMESTAMP NOT NULL"
                ")"
            ),
        )


async def pg_get(key: str) -> str | None:
    """读 PG 公共提示词；无记录返回 ``None``。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT content FROM system_prompts "
                        "WHERE key = :key",
                    ),
                    {"key": key},
                )
            ).first()
        return str(row[0]) if row is not None else None
    except Exception as e:  # pragma: no cover - DB 不可用
        logger.warning("system_prompt: pg_get(%s) failed: %s", key, e)
        return None


async def pg_set(key: str, content: str) -> None:
    """UPSERT 公共提示词到 PG（真源）。"""
    engine = await _get_engine()
    from sqlalchemy import text

    # ON CONFLICT 是 PG/sqlite 方言；MySQL 用 ON DUPLICATE KEY UPDATE。
    if engine.dialect.name == "mysql":
        upsert = (
            "ON DUPLICATE KEY UPDATE "
            "content = VALUES(content), updated_at = VALUES(updated_at)"
        )
    else:
        upsert = (
            "ON CONFLICT (key) DO UPDATE SET "
            "content = :content, updated_at = :ts"
        )

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO system_prompts (key, content, updated_at) "
                "VALUES (:key, :content, :ts) " + upsert,
            ),
            {"key": key, "content": content, "ts": datetime.now()},
        )


async def pg_delete(key: str) -> bool:
    """删除公共提示词；返回是否删除成功（不存在返回 False）。"""
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(
            text("DELETE FROM system_prompts WHERE key = :key"),
            {"key": key},
        )
    return result.rowcount > 0


async def pg_list_all() -> dict[str, str]:
    """PG 全量（key → content），列表接口用。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT key, content FROM system_prompts"),
                )
            ).all()
        return {str(r[0]): str(r[1]) for r in rows}
    except Exception as e:  # pragma: no cover - DB 不可用
        logger.warning("system_prompt: pg_list_all failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# API 端点
# ---------------------------------------------------------------------------


@system_prompt_router.get(
    "/global",
    response_model=SystemPromptResponse,
    summary="获取全局默认提示词",
)
async def get_global_prompt() -> SystemPromptResponse:
    """获取全局默认系统提示词。"""
    content = await pg_get("global")
    return SystemPromptResponse(
        key="global",
        content=content or "",
        is_default=True,
    )


@system_prompt_router.put(
    "/global",
    response_model=SystemPromptResponse,
    summary="更新全局默认提示词",
)
async def update_global_prompt(
    body: SystemPromptUpdateRequest,
    user_id: str = Depends(get_current_user_id),
) -> SystemPromptResponse:
    """更新全局默认系统提示词。"""
    await pg_set("global", body.content)
    logger.info("Updated global system prompt (%d chars)", len(body.content))
    return SystemPromptResponse(
        key="global",
        content=body.content,
        is_default=True,
    )


@system_prompt_router.delete(
    "/global",
    summary="删除全局默认提示词",
)
async def delete_global_prompt(
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """删除全局默认系统提示词（恢复为框架默认）。"""
    deleted = await pg_delete("global")
    logger.info("Deleted global system prompt")
    return {"deleted": deleted, "key": "global"}


@system_prompt_router.get(
    "/{agent_id}",
    response_model=SystemPromptResponse,
    summary="获取指定智能体的提示词",
)
async def get_agent_prompt(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> SystemPromptResponse:
    """获取指定智能体的自定义提示词。

    如果智能体没有自定义提示词，返回全局默认提示词。
    """
    content = await pg_get(agent_id)
    is_default = content is None

    # 如果没有自定义，返回全局默认
    if content is None:
        content = await pg_get("global")

    return SystemPromptResponse(
        key=agent_id,
        content=content or "",
        is_default=is_default,
    )


@system_prompt_router.put(
    "/{agent_id}",
    response_model=SystemPromptResponse,
    summary="更新指定智能体的提示词",
)
async def update_agent_prompt(
    agent_id: str,
    body: SystemPromptUpdateRequest,
    user_id: str = Depends(get_current_user_id),
) -> SystemPromptResponse:
    """更新指定智能体的自定义提示词。"""
    await pg_set(agent_id, body.content)
    logger.info(
        "Updated agent %s system prompt (%d chars)",
        agent_id,
        len(body.content),
    )
    return SystemPromptResponse(
        key=agent_id,
        content=body.content,
        is_default=False,
    )


@system_prompt_router.delete(
    "/{agent_id}",
    summary="删除指定智能体的自定义提示词",
)
async def delete_agent_prompt(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """删除指定智能体的自定义提示词（恢复为全局默认）。"""
    deleted = await pg_delete(agent_id)
    logger.info("Deleted agent %s custom prompt", agent_id)
    return {"deleted": deleted, "key": agent_id}


@system_prompt_router.post(
    "",
    response_model=SystemPromptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建系统提示词",
)
async def create_system_prompt(
    body: SystemPromptCreateRequest,
    user_id: str = Depends(get_current_user_id),
) -> SystemPromptResponse:
    """创建或覆盖系统提示词。

    如果 agent_id 为空，则设置全局默认提示词。
    """
    key = body.agent_id or "global"
    await pg_set(key, body.content)
    logger.info(
        "Created system prompt for %s (%d chars)",
        key,
        len(body.content),
    )
    return SystemPromptResponse(
        key=key,
        content=body.content,
        is_default=body.agent_id is None,
    )


@system_prompt_router.get(
    "",
    response_model=list[SystemPromptResponse],
    summary="列出所有系统提示词",
)
async def list_system_prompts(
    user_id: str = Depends(get_current_user_id),
) -> list[SystemPromptResponse]:
    """列出所有系统提示词（全局 + 各智能体自定义）。"""
    all_prompts = await pg_list_all()

    result = []
    # 全局默认
    result.append(SystemPromptResponse(
        key="global",
        content=all_prompts.get("global", ""),
        is_default=True,
    ))
    # 按智能体自定义
    for agent_id, content in all_prompts.items():
        if agent_id == "global":
            continue
        result.append(SystemPromptResponse(
            key=agent_id,
            content=content,
            is_default=False,
        ))
    return result


__all__ = ["system_prompt_router"]
