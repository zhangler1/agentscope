# -*- coding: utf-8 -*-
"""Per-agent /chat 并发上限管理 API。

PUT    /agents/chat-concurrency                — 设置上限（agent_id 在请求体）
GET    /agents/chat-concurrency                — 查询：不传 agent_id = 全部
                                                 已配置分页列表；传 agent_id =
                                                 单条详情。两者都只返回配置
                                                 信息（上限值、来源、设置时间），
                                                 不含实时并发数
DELETE /agents/chat-concurrency?agent_id=xxx   — 恢复不限（agent_id 走查询参数）

真源为 PG（``agent_chat_concurrency`` 表，重启不丢），运行时热层
（进程内缓存 + Redis）由 ``chat_concurrency_config`` 双写维护，
PUT 后下一个 ``POST /chat/`` 请求即按新上限拦截，无需重启。

鉴权与 404 口径同 ``agent_concurrency.py``（``get_current_user_id`` +
``storage.get_agent``）。列表不做可见性过滤（配置页由前端权限屏蔽，
仅管理员入口）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id

from bocomadp import chat_concurrency_config

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


async def _resolve_agent(
    request: Request,
    user_id: str,
    agent_id: str,
) -> Any:
    """按调用者 scope 解析 agent（与 agent_concurrency 一致）；不存在 → 404。"""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        agent = await storage.get_agent(user_id, agent_id)
    except Exception:  # noqa: BLE001
        agent = None
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


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
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    await _resolve_agent(request, user_id, body.agent_id)

    # 1. PG 真源（持久化，重启不丢）；名称按 id 关联 agents 表取快照存入，
    #    供列表按名称模糊查询（智能体改名后需重新 PUT 才会刷新）。
    names = await chat_concurrency_config.pg_agent_names([body.agent_id])
    await chat_concurrency_config.pg_upsert(
        body.agent_id, body.max_running, names.get(body.agent_id, ""),
    )

    # 2. 运行时热层（缓存必写，Redis 尽力而为；热路径直接读它）
    runtime_synced = await chat_concurrency_config.set_runtime(
        body.agent_id, body.max_running,
    )

    # 3. 闸机启停标志：存在任一配置即激活（不依赖 run_concurrency.enabled）
    chat_concurrency_config.mark_has_any(True)

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
    request: Request,
    agent_name: str | None = Query(default=None),
    pageNum: int = Query(default=1, ge=1),
    pageSize: int = Query(default=10, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
) -> dict:
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
    summary="恢复不限（删 PG 真源 + 删运行时热层）",
)
async def delete_chat_concurrency(
    request: Request,
    agent_id: str = Query(min_length=1),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    await _resolve_agent(request, user_id, agent_id)

    await chat_concurrency_config.pg_delete(agent_id)
    await chat_concurrency_config.delete_runtime(agent_id)
    if await chat_concurrency_config.pg_count() == 0:
        chat_concurrency_config.mark_has_any(False)

    logger.info("agent_chat_concurrency: %s -> reset to unlimited", agent_id)
    return {
        "agent_id": agent_id,
        "max_running": None,
    }


__all__ = ["agent_chat_concurrency_router"]
