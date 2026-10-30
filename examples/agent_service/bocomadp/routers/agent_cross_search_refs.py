# -*- coding: utf-8 -*-
"""Per-agent 跨知识搜索「引用别人的知识空间」管理 API。

GET    /agents/{agent_id}/cross-search-refs                                — 列出引用
PUT    /agents/{agent_id}/cross-search-refs                                — 全量同步引用（UPSERT 入参 + 删除多余，增删改合一）

与现有 cross-search-config（用户自己拥有的空间）接口完全独立、互不干扰；
引用按 (登录用户, 智能体) 隔离；运行时 cross_search 工具会把"自己的 + 引用的"
按 owner_user_code 分组多次调网关并合并结果。

space_name 由后端在 PUT 时调网关（queryPageKnSpace.do）按 owner 查企业
知识空间列表、用 space_code 匹配 spaceId 解析而来；查不到或网关异常存
空串，GET/PUT 响应原样返回，前端以"名字为空"识别手填的 space_code 有问题。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

from bocomadp.agent_cross_search_ref import (
    cross_search_ref_list,
    cross_search_ref_sync,
    fetch_space_names,
)

logger = logging.getLogger("bocomadp.agent_cross_search_refs")

agent_cross_search_refs_router = APIRouter(
    prefix="/agents",
    tags=["agent-cross-search-refs"],
)


async def _resolve_agent(request: Request, user_id: str, agent_id: str) -> Any:
    """复用 cross-search-config 路由的鉴权：智能体不存在则 None。"""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        return None
    try:
        return await storage.get_agent(user_id, agent_id)
    except Exception:  # noqa: BLE001
        return None


async def _resolve_space_names(
    refs: list[RefSpaceCreate],
) -> dict[tuple[str, str], str]:
    """按 owner 串行调网关，解析每条引用的空间名称。

    查名网关没有单查接口，只能按 userCode + spaceCategory 拉某 owner 的全部
    空间列表；同一 owner 无论引用几个空间都只调一次网关，再用 spaceId 逐个
    匹配名字。返回 ``{(owner_user_code, space_code): space_name}``，查不到为 ""。
    """
    if not refs:
        return {}
    names: dict[tuple[str, str], str] = {}
    for owner_user_code in {r.owner_user_code for r in refs}:
        space_map = await fetch_space_names(owner_user_code)
        for r in refs:
            if r.owner_user_code == owner_user_code:
                names[(owner_user_code, r.space_code)] = space_map.get(
                    r.space_code, ""
                )
    return names


class RefSpaceCreate(BaseModel):
    """全量同步里的一项：被引用的空间（创建者 + 空间 id）。"""

    owner_user_code: str = Field(
        min_length=1,
        max_length=64,
        description="被引用空间的创建者 userCode（别人的钥匙）",
    )
    space_code: str = Field(
        min_length=1,
        max_length=128,
        description="被引用的知识空间 id",
    )


def _item_view(it: Any) -> dict:
    return {
        "owner_user_code": it.owner_user_code,
        "space_code": it.space_code,
        "space_name": it.space_name,
        "created_at": it.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


@agent_cross_search_refs_router.get(
    "/{agent_id}/cross-search-refs",
    summary="列出当前用户在该智能体引用的所有知识空间",
)
async def list_cross_search_refs(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> list[dict]:
    agent = await _resolve_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    items = await cross_search_ref_list(user_id, agent_id)
    return [_item_view(it) for it in items]


@agent_cross_search_refs_router.put(
    "/{agent_id}/cross-search-refs",
    summary="全量同步引用知识空间（PUT=覆盖：入参即最终态，缺的自动删除）",
)
async def set_cross_search_refs(
    agent_id: str,
    body: list[RefSpaceCreate],
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    agent = await _resolve_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    names = await _resolve_space_names(body)
    refs = [
        (it.owner_user_code, it.space_code, names[(it.owner_user_code, it.space_code)])
        for it in body
    ]
    await cross_search_ref_sync(user_id, agent_id, refs)

    items = await cross_search_ref_list(user_id, agent_id)
    logger.info(
        "agent_cross_search_refs: %s synced %d ref(s) (user=%s)",
        agent_id,
        len(items),
        user_id,
    )
    return {
        "agent_id": agent_id,
        "items": [_item_view(it) for it in items],
    }


__all__ = ["agent_cross_search_refs_router"]
