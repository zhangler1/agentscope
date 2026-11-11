# -*- coding: utf-8 -*-
"""Per-agent /chat 并发上限管理 API。

PUT    /agents/chat-concurrency                — 设置上限（agent_id 在请求体）
GET    /agents/chat-concurrency                — 已配置智能体的分页列表，
                                                 支持 agent_name 模糊查询；
                                                 只返回配置信息（上限值、
                                                 来源、设置时间），不含实时并发数
DELETE /agents/chat-concurrency?agent_id=xxx   — 删除配置，回到默认 1 路（agent_id 走查询参数）

真源为 PG（``agent_chat_concurrency`` 表，重启不丢），运行时热层
（进程内缓存 + Redis）由 ``chat_concurrency_config`` 双写维护，
PUT 后下一个 ``POST /chat/`` 请求即按新上限拦截，无需重启。

鉴权：三个接口均限 ``reviewer`` 角色（与审批页面、智能体全量列表同一
判据），非管理员 403；``user_id`` 取自请求头 ``X-User-ID``。前端按
``GET /agent/market/my-role`` 的结果决定是否展示配置入口，此处作后端
兜底。列表不做可见性过滤（跨用户全量，仅管理员可见）；PUT/DELETE 的
存在性校验也跨用户（按 agent_id 直查 ``agents`` 表，不带归属过滤），
管理员可配置所有已通过审批的智能体，不受智能体归属限制。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

from bocomadp import chat_concurrency_config, role_store

logger = logging.getLogger("bocomadp.agent_chat_concurrency")

agent_chat_concurrency_router = APIRouter(
    prefix="/agents",
    tags=["agent-chat-concurrency"],
)


class ChatConcurrencyRequest(BaseModel):
    """设置智能体同时对话数上限（agent_id 与 max_running 同在请求体）。"""

    agent_id: str = Field(
        min_length=1,
        description="智能体 ID（主键口径，不支持按名称设置）",
    )
    max_running: int = Field(
        ge=0,
        description="同时对话数上限；0 = 显式不限",
    )


async def _require_admin(user_id: str) -> None:
    """并发配置的管理员门禁：无 reviewer 角色 → 403。

    判据与审核页面、智能体全量列表一致（role_store 的 reviewer 角色），
    user_id 取自 X-User-ID；前端按 my-role 结果控制入口，此处兜底。
    """
    if not await role_store.has_role(user_id, role_store.REVIEWER_ROLE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有管理员（reviewer 角色）可以配置智能体并发路数。",
        )


async def _resolve_agent(agent_id: str) -> str:
    """跨用户按 agent_id 取智能体名称（管理员可管理所有人的智能体）。

    直接查 ``agents`` 表按主键取名称，不带 user_id 过滤——与管理员的
    智能体全量列表同口径。查不到或查库失败一律 404，不泄露归属信息。
    """
    names = await chat_concurrency_config.pg_agent_names([agent_id])
    if agent_id not in names:
        raise HTTPException(status_code=404, detail="Agent not found")
    return names[agent_id]


def _fmt_ts(ts: float | None) -> str | None:
    """epoch 秒 → ISO 时间串（接口展示口径）。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(
        timespec="seconds",
    )


@agent_chat_concurrency_router.put(
    "/chat-concurrency",
    summary="设置智能体同时对话数上限（写 PG 真源 + 运行时热层）",
)
async def set_chat_concurrency(
    body: ChatConcurrencyRequest,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    await _require_admin(user_id)
    agent_name = await _resolve_agent(body.agent_id)

    # 仅审核通过的智能体允许配置并发上限：未过审或审批状态暂不可用
    # 一律拒绝、不落库（后者防库抖动窗口内脏配置写入）。
    if not await chat_concurrency_config.is_agent_approved(body.agent_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅审核通过的智能体可配置并发路数；审批状态暂不可用时请稍后重试。",
        )

    # 1. PG 真源（持久化，重启不丢）；名称由 _resolve_agent 关联 agents
    #    表取快照存入，供列表按名称模糊查询（改名后需重新 PUT 才刷新）。
    await chat_concurrency_config.pg_upsert(
        body.agent_id, body.max_running, agent_name,
    )

    # 2. 运行时热层（缓存必写，Redis 尽力而为；热路径直接读它）
    runtime_synced = await chat_concurrency_config.set_runtime(
        body.agent_id, body.max_running,
    )

    logger.info(
        "agent_chat_concurrency: %s -> max_running=%d (runtime_synced=%s)",
        body.agent_id,
        body.max_running,
        runtime_synced,
    )
    return {
        "agent_id": body.agent_id,
        "max_running": body.max_running,
        "runtime_synced": runtime_synced,
    }


@agent_chat_concurrency_router.get(
    "/chat-concurrency",
    summary="查询并发配置：分页列表，按 agent_name 模糊查询（agent_name 为空=全部）",
)
async def get_chat_concurrency(
    agent_name: str | None = Query(default=None),
    pageNum: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    await _require_admin(user_id)
    return await _list_all(pageNum, pageSize, agent_name)


async def _list_all(
    page_num: int,
    page_size: int,
    agent_name: str | None = None,
) -> dict:
    """全部已配置智能体的分页列表（仅配置信息，不含实时并发）。

    支持按 ``agent_name`` 模糊查询（``agent_name`` 入参）；排序固定：updated_at 倒序
    （最近设置的在前），agent_id 兜底；未配置过的智能体不出现；翻页超出范围
    返回空 items 不报错。
    """
    rows, total = await chat_concurrency_config.pg_list_filtered(
        agent_name, page_num, page_size,
    )
    items = []
    for agent_id, size, ts, name in rows:
        _value, source = await chat_concurrency_config.get_effective(agent_id)
        items.append(
            {
                "agent_id": agent_id,
                "agent_name": name,
                "max_running": size,
                "source": source,
                "updated_at": _fmt_ts(ts),
            },
        )
    return {
        "total": total,
        "page_num": page_num,
        "page_size": page_size,
        "items": items,
    }


@agent_chat_concurrency_router.delete(
    "/chat-concurrency",
    summary="删除配置，回到默认 1 路（删 PG 真源 + 删运行时热层）",
)
async def delete_chat_concurrency(
    agent_id: str = Query(min_length=1),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    await _require_admin(user_id)
    await _resolve_agent(agent_id)

    await chat_concurrency_config.clear_agent_config(agent_id)

    logger.info("agent_chat_concurrency: %s -> reset to default limit 1", agent_id)
    return {
        "agent_id": agent_id,
        "max_running": None,  # 无显式配置，闸机按默认 1 路处理
    }


__all__ = ["agent_chat_concurrency_router"]
