# -*- coding: utf-8 -*-
"""Per-agent 跨知识搜索配置存储：MySQL 表（仿 memory_config.py）。

写入路径（PUT /agents/{id}/cross-search-config）：MySQL UPSERT。
读取路径（cross_search 工具运行时）：MySQL 查询，无记录返回 None，
工具回退到 config.yaml 默认值。

cross_search 配置仅被管理 API 和工具运行时读写，不在请求级
热路径上，因此不做 Redis 热层。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from bocomadp.config import get_app_config

_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS agent_cross_search_configs ("
    "user_id VARCHAR(255) NOT NULL, "
    "agent_id VARCHAR(255) NOT NULL, "
    "user_code VARCHAR(255) NOT NULL DEFAULT '', "
    "search_type VARCHAR(16) NOT NULL DEFAULT '0', "
    "space_code_list TEXT, "
    "team_space_code_list TEXT, "
    "psnl_space_code_id VARCHAR(255) NOT NULL DEFAULT '', "
    "psnl_category_id_list TEXT, "
    "customized_tag_list TEXT, "
    "text_top_n INTEGER, "
    "vector_top_n INTEGER, "
    "updated_at DATETIME NOT NULL, "
    "PRIMARY KEY (user_id, agent_id)"
    ")"
)


class AgentCrossSearchConfig(BaseModel):
    """智能体级跨知识搜索配置。"""

    user_code: str = Field(default="", description="用户编码")
    search_type: str = Field(default="0", description="检索类型：0=混合，1=全文，2=向量")
    space_code_list: list[str] = Field(default_factory=list, description="场景知识空间代码列表")
    team_space_code_list: list[str] = Field(default_factory=list, description="团队知识空间代码列表")
    psnl_space_code_id: str = Field(default="", description="个人知识空间代码ID")
    psnl_category_id_list: list[str] = Field(default_factory=list, description="个人知识分类ID列表")
    customized_tag_list: list[str] = Field(default_factory=list, description="自定义标签列表")
    text_top_n: int | None = Field(default=None, description="全文检索返回条数")
    vector_top_n: int | None = Field(default=None, description="向量检索返回条数")


_engine: Any = None
_initialized_for: Any = None


async def _get_engine() -> Any:
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        _engine = create_async_engine(
            get_app_config().db.url,
            pool_pre_ping=True,
        )
    return _engine


async def _ensure_table() -> None:
    global _initialized_for
    engine = await _get_engine()
    if _initialized_for is engine:
        return
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_TABLE_SQL))
    _initialized_for = engine


def _dump_list(val: list[str]) -> str:
    import json
    return json.dumps(val, ensure_ascii=False)


def _load_list(val: Any) -> list[str]:
    import json
    if isinstance(val, list):
        return val
    if val is None:
        return []
    if isinstance(val, str):
        try:
            result = json.loads(val)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


async def cross_search_config_upsert(
    user_id: str,
    agent_id: str,
    config: AgentCrossSearchConfig,
) -> None:
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agent_cross_search_configs "
                "(user_id, agent_id, user_code, search_type, "
                " space_code_list, team_space_code_list, psnl_space_code_id, "
                " psnl_category_id_list, customized_tag_list, "
                " text_top_n, vector_top_n, updated_at) "
                "VALUES (:user_id, :agent_id, :user_code, :search_type, "
                " :space_code_list, :team_space_code_list, :psnl_space_code_id, "
                " :psnl_category_id_list, :customized_tag_list, "
                " :text_top_n, :vector_top_n, :ts) "
                "ON DUPLICATE KEY UPDATE "
                "user_code = VALUES(user_code), "
                "search_type = VALUES(search_type), "
                "space_code_list = VALUES(space_code_list), "
                "team_space_code_list = VALUES(team_space_code_list), "
                "psnl_space_code_id = VALUES(psnl_space_code_id), "
                "psnl_category_id_list = VALUES(psnl_category_id_list), "
                "customized_tag_list = VALUES(customized_tag_list), "
                "text_top_n = VALUES(text_top_n), "
                "vector_top_n = VALUES(vector_top_n), "
                "updated_at = VALUES(updated_at)"
            ),
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "user_code": config.user_code,
                "search_type": config.search_type,
                "space_code_list": _dump_list(config.space_code_list),
                "team_space_code_list": _dump_list(config.team_space_code_list),
                "psnl_space_code_id": config.psnl_space_code_id,
                "psnl_category_id_list": _dump_list(config.psnl_category_id_list),
                "customized_tag_list": _dump_list(config.customized_tag_list),
                "text_top_n": config.text_top_n,
                "vector_top_n": config.vector_top_n,
                "ts": datetime.now(),
            },
        )


async def cross_search_config_get(
    user_id: str,
    agent_id: str,
) -> AgentCrossSearchConfig | None:
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT user_code, search_type, space_code_list, "
                    "team_space_code_list, psnl_space_code_id, "
                    "psnl_category_id_list, customized_tag_list, "
                    "text_top_n, vector_top_n "
                    "FROM agent_cross_search_configs "
                    "WHERE user_id = :user_id AND agent_id = :agent_id"
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )
        ).mappings().first()
    if row is None:
        return None
    text_top_n_val = row["text_top_n"]
    vector_top_n_val = row["vector_top_n"]
    return AgentCrossSearchConfig(
        user_code=row["user_code"],
        search_type=row["search_type"],
        space_code_list=_load_list(row["space_code_list"]),
        team_space_code_list=_load_list(row["team_space_code_list"]),
        psnl_space_code_id=row["psnl_space_code_id"],
        psnl_category_id_list=_load_list(row["psnl_category_id_list"]),
        customized_tag_list=_load_list(row["customized_tag_list"]),
        text_top_n=int(text_top_n_val) if text_top_n_val is not None else None,
        vector_top_n=int(vector_top_n_val) if vector_top_n_val is not None else None,
    )


async def cross_search_config_delete(user_id: str, agent_id: str) -> bool:
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "DELETE FROM agent_cross_search_configs "
                "WHERE user_id = :user_id AND agent_id = :agent_id",
            ),
            {"user_id": user_id, "agent_id": agent_id},
        )
    return result.rowcount > 0


__all__ = [
    "AgentCrossSearchConfig",
    "cross_search_config_upsert",
    "cross_search_config_get",
    "cross_search_config_delete",
]
