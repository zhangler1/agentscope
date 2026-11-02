# -*- coding: utf-8 -*-
"""Per-agent 跨知识搜索「引用别人的知识空间」存储：MySQL 表。

现有 ``agent_cross_search_configs`` 存"用户自己拥有的知识空间"（主键
user_id+agent_id，user_code 单值）。本模块新增独立的引用表，支持同一用户
在同一智能体上引用多个不同创建者（owner_user_code）的知识空间，运行时由
cross_search 工具按 owner 分组、多次调网关合并结果。

设计约束：引用表与配置表完全解耦，现有 cross-search-config 接口/表/模型
零改动（见 docs/agent-cross-search-ref-design.md）。

空间名称（space_name）：引用登记时后端调网关 queryPageKnSpace.do 按
owner 查其创建的企业知识空间（spaceCategory=2）列表，用 space_code 匹配
响应里的 spaceId 取 spaceName 落库。查不到或网关异常一律存空串——空名字
是前端识别"用户手填的 space_code 有问题"的信号，不阻断登记。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel

from bocomadp.config import get_app_config
from bocomadp.config.cross_search_space_config import (
    get_cross_search_space_config,
)

logger = logging.getLogger("bocomadp.agent_cross_search_ref")

# owner 对齐接口上限 64，space_code/space_name 统一收 128；agent_id 与
# agent_cross_search_configs 表对齐收 255。
# 四列联合唯一键 (128+255+64+128)*4=2300 字节，低于 MySQL 3072 字节索引上限。
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS agent_cross_search_refs ("
    "id BIGINT AUTO_INCREMENT PRIMARY KEY, "
    "user_id VARCHAR(128) NOT NULL, "
    "agent_id VARCHAR(255) NOT NULL, "
    "owner_user_code VARCHAR(64) NOT NULL, "
    "space_code VARCHAR(128) NOT NULL, "
    "space_name VARCHAR(128) NOT NULL DEFAULT '', "
    "created_at DATETIME NOT NULL, "
    "UNIQUE KEY uq_ref (user_id, agent_id, owner_user_code, space_code), "
    "KEY idx_user_agent (user_id, agent_id)"
    ")"
)

# 空间名称查询网关地址与超时取 config.yaml 的 cross_search_space 节点
# （与其它企业工具一致：一个网关一个配置节点，切换环境只改 yaml）。


class RefSpaceItem(BaseModel):
    """一条「引用别人的知识空间」记录。"""

    user_id: str
    agent_id: str
    owner_user_code: str
    space_code: str
    space_name: str = ""
    created_at: datetime


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


async def fetch_space_names(owner_user_code: str) -> dict[str, str]:
    """调网关查该 owner 创建的企业知识空间，返回 ``{spaceId: spaceName}``。

    网关没有"按单个 spaceId 查名"的接口，只能按 userCode + spaceCategory=2
    拉全量再自己匹配。任何异常（网络/超时/TRAN_SUCCESS!=1）都吞掉返回
    空映射——名字缺失只影响展示，不应阻断引用登记。
    """
    req_message = {
        "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {
            "param": {
                "userCode": owner_user_code,
                "pageNum": 1,
                "pageSize": 1000,
                "offSet": 0,
                "spaceCategory": 2,
            }
        },
    }
    headers = {
        "Accept": "*/*",
        "userCode": owner_user_code,
        "Jumpcloud-Env": "BASE",
    }
    cfg = get_cross_search_space_config()
    if not cfg.api_url:
        raise ValueError(
            "cross_search_space.api_url is required. 请在 config.yaml 中配置。",
        )
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            resp = await client.post(
                cfg.api_url,
                headers=headers,
                files={
                    "REQ_MESSAGE": (
                        None,
                        json.dumps(req_message, ensure_ascii=False),
                    )
                },
            )
            resp.raise_for_status()
        payload = resp.json()
        if payload.get("RSP_HEAD", {}).get("TRAN_SUCCESS") != "1":
            logger.warning(
                "queryPageKnSpace TRAN_SUCCESS != 1 (owner=%s)",
                owner_user_code,
            )
            return {}
        data = payload.get("RSP_BODY", {}).get("result", {}).get("data") or []
        space_names: dict[str, str] = {}
        for item in data:
            info = item.get("spaceInfo") if isinstance(item, dict) else None
            if not isinstance(info, dict) or not info.get("spaceId"):
                continue
            space_names[str(info["spaceId"])] = info.get("spaceName") or ""
        return space_names
    except Exception:  # noqa: BLE001
        logger.warning(
            "queryPageKnSpace failed (owner=%s)", owner_user_code, exc_info=True
        )
        return {}


async def cross_search_ref_upsert(
    user_id: str,
    agent_id: str,
    owner_user_code: str,
    space_code: str,
    space_name: str = "",
) -> None:
    """新增/更新一条引用。靠 UNIQUE(user_id,agent_id,owner_user_code,space_code)
    防重，重复引用同一空间不会报错（刷新 created_at 和 space_name）。"""
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agent_cross_search_refs "
                "(user_id, agent_id, owner_user_code, space_code, space_name, "
                "created_at) "
                "VALUES (:user_id, :agent_id, :owner_user_code, :space_code, "
                ":space_name, :ts) "
                "ON DUPLICATE KEY UPDATE created_at = VALUES(created_at), "
                "space_name = VALUES(space_name)"
            ),
            {
                "user_id": user_id,
                "agent_id": agent_id,
                "owner_user_code": owner_user_code,
                "space_code": space_code,
                "space_name": space_name,
                "ts": datetime.now(),
            },
        )


async def cross_search_ref_list(
    user_id: str,
    agent_id: str,
) -> list[RefSpaceItem]:
    """列出该用户在该智能体引用的所有空间（按登录用户 + 智能体隔离）。"""
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT user_id, agent_id, owner_user_code, space_code, "
                    "space_name, created_at "
                    "FROM agent_cross_search_refs "
                    "WHERE user_id = :user_id AND agent_id = :agent_id "
                    "ORDER BY created_at ASC, id ASC"
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )
        ).mappings().all()
    return [
        RefSpaceItem(
            user_id=r["user_id"],
            agent_id=r["agent_id"],
            owner_user_code=r["owner_user_code"],
            space_code=r["space_code"],
            space_name=r.get("space_name") or "",
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def cross_search_ref_sync(
    user_id: str,
    agent_id: str,
    refs: list[tuple[str, str, str]],
) -> None:
    """全量同步该 (用户, 智能体) 的引用集合到 refs（入参即最终态）。

    refs 每项为 ``(owner_user_code, space_code, space_name)``：

    - 入参里有的 -> upsert（新增或刷新时间/名字）；
    - 库里该 (用户, 智能体) 存在、但不在入参里的 -> 删除；
    - refs 为空列表 -> 清空该 (用户, 智能体) 的全部引用。
    与 cross-search-config 的「PUT 整条覆盖」范式一致。
    """
    await _ensure_table()
    engine = await _get_engine()
    from sqlalchemy import text

    # 1) upsert 入参每条
    for owner_user_code, space_code, space_name in refs:
        await cross_search_ref_upsert(
            user_id, agent_id, owner_user_code, space_code, space_name
        )

    # 2) 删除库里不在入参中的（全量覆盖的「删」）
    if refs:
        placeholders = ", ".join(
            f"(:o{i}, :s{i})" for i in range(len(refs))
        )
        params: dict[str, Any] = {"user_id": user_id, "agent_id": agent_id}
        for i, (o, s, _n) in enumerate(refs):
            params[f"o{i}"] = o
            params[f"s{i}"] = s
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM agent_cross_search_refs "
                    "WHERE user_id = :user_id AND agent_id = :agent_id "
                    f"AND (owner_user_code, space_code) NOT IN ({placeholders})"
                ),
                params,
            )
    else:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "DELETE FROM agent_cross_search_refs "
                    "WHERE user_id = :user_id AND agent_id = :agent_id"
                ),
                {"user_id": user_id, "agent_id": agent_id},
            )


__all__ = [
    "RefSpaceItem",
    "cross_search_ref_upsert",
    "cross_search_ref_list",
    "cross_search_ref_sync",
    "fetch_space_names",
]
