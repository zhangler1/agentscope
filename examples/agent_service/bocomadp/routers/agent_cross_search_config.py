# -*- coding: utf-8 -*-
"""Per-agent 跨知识搜索配置管理 API。

GET    /agents/{agent_id}/cross-search-config   — 查询配置
PUT    /agents/{agent_id}/cross-search-config   — 设置配置（PG UPSERT）
DELETE /agents/{agent_id}/cross-search-config   — 删除配置（恢复 config.yaml 默认）
GET    /agents/{agent_id}/cross-search-tags     — 查询跨知识搜索可用标签

配置存储在 PG（agent_cross_search_configs 表，重启不丢），
cross_search 工具运行时直接查 PG，无记录则回退到 config.yaml 默认值。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from agentscope.app.deps import get_current_user_id

from bocomadp.agent_cross_search_config import (
    AgentCrossSearchConfig,
    cross_search_config_delete,
    cross_search_config_get,
    cross_search_config_upsert,
)
from bocomadp.market_audit import log_audit
from bocomadp.reviewer_access import reviewer_resolve_agent

logger = logging.getLogger("bocomadp.agent_cross_search_config")

agent_cross_search_config_router = APIRouter(
    prefix="/agents",
    tags=["agent-cross-search-config"],
)


async def _resolve_agent(
    request: Request,
    user_id: str,
    agent_id: str,
) -> Any:
    resolved = await reviewer_resolve_agent(request, user_id, agent_id)
    if resolved is None:
        return None
    return resolved[1]


@agent_cross_search_config_router.get(
    "/{agent_id}/cross-search-config",
    summary="查询智能体跨知识搜索配置",
)
async def get_agent_cross_search_config(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    resolved = await reviewer_resolve_agent(request, user_id, agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner_id, _ = resolved

    config = await cross_search_config_get(owner_id, agent_id)
    if config is None:
        return {
            "agent_id": agent_id,
            "configured": False,
            "config": None,
        }
    return {
        "agent_id": agent_id,
        "configured": True,
        "config": config.model_dump(),
    }


@agent_cross_search_config_router.put(
    "/{agent_id}/cross-search-config",
    summary="设置智能体跨知识搜索配置",
)
async def set_agent_cross_search_config(
    agent_id: str,
    body: AgentCrossSearchConfig,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    resolved = await reviewer_resolve_agent(request, user_id, agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner_id, _ = resolved

    await cross_search_config_upsert(owner_id, agent_id, body)
    # 管理员代他人配置跨知识搜索留痕
    log_audit(
        user_id,
        "cross_search_config_set_by_admin",
        target=agent_id,
        detail=f"管理员配置跨知识搜索(owner={owner_id})",
    )

    logger.info(
        "agent_cross_search_config: %s configured (user_code=%s, space_codes=%s)",
        agent_id,
        body.user_code,
        body.space_code_list,
    )
    return {
        "agent_id": agent_id,
        "configured": True,
        "config": body.model_dump(),
    }


@agent_cross_search_config_router.delete(
    "/{agent_id}/cross-search-config",
    summary="删除智能体跨知识搜索配置（恢复 config.yaml 默认）",
)
async def reset_agent_cross_search_config(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    resolved = await reviewer_resolve_agent(request, user_id, agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner_id, _ = resolved

    deleted = await cross_search_config_delete(owner_id, agent_id)
    # 管理员代他人重置跨知识搜索留痕
    log_audit(
        user_id,
        "cross_search_config_reset_by_admin",
        target=agent_id,
        detail=f"管理员重置跨知识搜索(owner={owner_id})",
    )

    logger.info(
        "agent_cross_search_config: %s reset to default (deleted=%s)",
        agent_id,
        deleted,
    )
    return {
        "agent_id": agent_id,
        "configured": False,
    }


__all__ = ["agent_cross_search_config_router"]


@agent_cross_search_config_router.get(
    "/{agent_id}/cross-search-tags",
    summary="查询跨知识搜索可用标签",
)
async def get_cross_search_tags(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    agent = await _resolve_agent(request, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    tag_api_url = "http://53.192.28.254:23674/EUVD.EUVD-OPENAPI.V-1.0/queryCustomTags.do"

    req_message = {
        "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {
            "param": {
                "sourceChannel": "0001",
                "tagName": "",
                "spaceId": "SP1000011",
                "pageNum": 1,
                "pageSize": 100,
            }
        },
    }
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Api-Key": "534954303030312D31373834353338373430323230",
        "Connection": "keep-alive",
        "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                tag_api_url,
                headers=headers,
                files={"REQ_MESSAGE": (None, json.dumps(req_message, ensure_ascii=False))},
            )
            response.raise_for_status()

        payload = response.json()
        rsp_head = payload.get("RSP_HEAD", {})
        if rsp_head.get("TRAN_SUCCESS") != "1":
            logger.warning("Cross-search tags API returned failure: %s", rsp_head)
            return {"success": True, "tags": []}

        result = payload.get("RSP_BODY", {}).get("result", {})
        data = result.get("data", [])
        tags = [
            {"tagId": item.get("tagId", ""), "tagName": item.get("tagName", "")}
            for item in data
            if item.get("tagName")
        ]
        return {"success": True, "tags": tags}
    except Exception as e:
        logger.error("Failed to fetch cross-search tags: %s", e, exc_info=True)
        return {"success": False, "tags": []}
