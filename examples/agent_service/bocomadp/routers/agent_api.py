# -*- coding: utf-8 -*-
"""智能体记忆字段 API 包裹路由。

覆盖框架内置 ``/agent/`` 的 4 个端点（GET/POST/PATCH/DELETE），
在请求/响应中增加 4 个记忆字段（见设计文档 §4）：

- ``memory_update_prompt``  — 记忆更新提示词
- ``memory_enabled``        — 记忆开关
- ``memory_type``           — 0=程序性记忆，1=事务性记忆
- ``memory_update_rounds``  — 每 N 轮对话触发记忆更新

包裹 handler 直接调用专家团 endpoint 函数（``bocomadp.routers.agent``，
即迁移后的 expert-team 实现），行为（权限 / 团队入队 / 404/409/422 语义）
零漂移；记忆字段读写侧边存储（``bocomadp.memory_config``）。

装配：``main.py`` 在 ``create_app`` 之后调用
``install_agent_memory_router(app)`` —— 路由前插覆盖同名路径，并移除
框架被覆盖的 4 条路由（保持 OpenAPI 无重复条目）。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from bocomadp.routers.agent import (
    create_agent as _core_create_agent,
    delete_agent as _core_delete_agent,
    list_agents as _core_list_agents,
    update_agent as _core_update_agent,
)
from bocomadp.routers._schema.agent import (
    CreateAgentRequest,
    CreateAgentResponse,
    ListAgentsResponse,
    TeamAgentView,
    UpdateAgentRequest,
)
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_session_service,
    get_storage,
)

from bocomadp import memory_config

logger = logging.getLogger("bocomadp.agent_api")

agent_api_router = APIRouter(prefix="/agent", tags=["agent-memory"])


# ------------------------------------------------------------------
# 请求 / 响应模型
# ------------------------------------------------------------------


class CreateAgentRequestWithMemory(CreateAgentRequest):
    """继承框架 CreateAgentRequest，+4 个可选记忆字段（带默认值）。"""

    memory_update_prompt: str = ""
    memory_enabled: bool = False
    memory_type: Literal[0, 1] = 0
    memory_update_rounds: int = Field(default=10, ge=0)


class UpdateAgentRequestWithMemory(UpdateAgentRequest):
    """继承框架 UpdateAgentRequest；4 记忆字段可选，None = 不改
    （与框架 PATCH 的 exclude_none 语义一致）；传 "" 可清空 prompt。"""

    memory_update_prompt: str | None = None
    memory_enabled: bool | None = None
    memory_type: Literal[0, 1] | None = None
    memory_update_rounds: int | None = Field(default=None, ge=0)


class CreateAgentResponseWithMemory(BaseModel):
    """创建响应：agent_id + 4 记忆字段回显。"""

    agent_id: str
    memory_update_prompt: str = ""
    memory_enabled: bool = False
    memory_type: int = 0
    memory_update_rounds: int = 10


class AgentViewWithMemory(TeamAgentView):
    """专家团 TeamAgentView（含 is_team / parent_agent_id / is_self_built）
    + 4 记忆字段（列表 / 更新响应合并）。"""

    memory_update_prompt: str = ""
    memory_enabled: bool = False
    memory_type: int = 0
    memory_update_rounds: int = 10


class ListAgentsResponseWithMemory(BaseModel):
    """列表响应：AgentViewWithMemory 列表 + total。"""

    agents: list[AgentViewWithMemory]
    total: int


# ------------------------------------------------------------------
# 包裹 handlers —— 直接调用框架原始 endpoint 函数，行为零漂移
# ------------------------------------------------------------------


def _extract_memory(body: BaseModel) -> memory_config.MemoryConfig:
    """从请求体提取 4 记忆字段为 MemoryConfig。"""
    return memory_config.MemoryConfig(
        memory_update_prompt=body.memory_update_prompt,
        memory_enabled=body.memory_enabled,
        memory_type=body.memory_type,
        memory_update_rounds=body.memory_update_rounds,
    )


def _memory_defaults() -> dict:
    """无侧边记录时的默认记忆字段。"""
    return memory_config.MemoryConfig().model_dump()


@agent_api_router.post(
    "/",
    response_model=CreateAgentResponseWithMemory,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new agent (with memory fields)",
)
async def create_agent_with_memory(
    body: CreateAgentRequestWithMemory,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    storage=Depends(get_storage),
    access=Depends(get_resource_access_service),
) -> CreateAgentResponseWithMemory:
    """创建智能体：框架原逻辑 + 记忆字段侧边落库（失败回滚）。"""
    mem = _extract_memory(body)
    framework_body = CreateAgentRequest(
        **body.model_dump(
            exclude={
                "memory_update_prompt",
                "memory_enabled",
                "memory_type",
                "memory_update_rounds",
            },
        ),
    )
    created: CreateAgentResponse = await _core_create_agent(
        body=framework_body,
        user_id=user_id,
        storage=storage,
        access=access,
    )
    try:
        await memory_config.memory_upsert(user_id, created.agent_id, mem)
    except Exception:
        # best-effort 回滚：删除刚创建的 agent，避免"存在但记忆配置丢失"
        logger.exception("memory upsert failed for %s; rolling back", created.agent_id)
        try:
            await storage.delete_agent(user_id, created.agent_id)
        except Exception:
            logger.exception("rollback delete failed for %s", created.agent_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent created but memory config write failed.",
        )
    return CreateAgentResponseWithMemory(
        agent_id=created.agent_id,
        **mem.model_dump(),
    )


@agent_api_router.get(
    "/",
    response_model=ListAgentsResponseWithMemory,
    summary="List all agents (with memory fields)",
)
async def list_agents_with_memory(
    parent_agent_id: str | None = None,
    is_team: bool | None = Query(
        default=None,
        description=(
            "Optional top-level filter: `true` returns only expert-team "
            "leaders, `false` only plain agents. Omit to list all."
        ),
    ),
    invitable: bool | None = Query(
        default=None,
        description="Optional filter: `true` returns only agents whose "
                    "invite_config.invitable is enabled, `false` only disabled ones.",
    ),
    page_num: int = Query(
        default=1,
        ge=1,
        alias="pageNum",
        description="Page number, 1-based.",
    ),
    page_size: int = Query(
        default=5,
        ge=1,
        le=100,
        alias="pageSize",
        description="Page size (items per page), 1-100.",
    ),
    user_id: str = Depends(get_current_user_id),
    storage=Depends(get_storage),
    access=Depends(get_resource_access_service),
) -> ListAgentsResponseWithMemory:
    """列表查询：框架原逻辑（含分页）+ 每项合并记忆字段（无记录用默认值）。

    storage 必须显式传给核心实现：list_agents 在 parent_agent_id 非空
    时要查 expert_team_relations 表过滤成员，顶层列表也要隐藏成员，
    都依赖真存储。漏传会拿到 Depends 占位对象（无 _session_factory），
    get_team 静默返回 None，带 parent 的列表就永远变空。

    分页参数透传给核心：total 为分页前的完整数量（与 agents 当前页
    条数可能不同），前端可据此算总页数。
    """
    result: ListAgentsResponse = await _core_list_agents(
        parent_agent_id=parent_agent_id,
        is_team=is_team,
        invitable=invitable,
        page_num=page_num,
        page_size=page_size,
        user_id=user_id,
        storage=storage,
        access=access,
    )
    merged: list[AgentViewWithMemory] = []
    for view in result.agents:
        mem = await memory_config.memory_get(user_id, view.id)
        mem_fields = mem.model_dump() if mem is not None else _memory_defaults()
        merged.append(
            AgentViewWithMemory.model_validate(
                {**view.model_dump(), **mem_fields},
            ),
        )
    return ListAgentsResponseWithMemory(agents=merged, total=result.total)


@agent_api_router.patch(
    "/{agent_id}",
    response_model=AgentViewWithMemory,
    summary="Update an agent (with memory fields)",
)
async def update_agent_with_memory(
    agent_id: str,
    body: UpdateAgentRequestWithMemory,
    user_id: str = Depends(get_current_user_id),
    storage=Depends(get_storage),
    access=Depends(get_resource_access_service),
) -> AgentViewWithMemory:
    """更新智能体：框架原逻辑 + 记忆字段侧边更新（None 字段跳过）。"""
    core = body.model_dump(
        exclude_none=True,
        exclude={
            "memory_update_prompt",
            "memory_enabled",
            "memory_type",
            "memory_update_rounds",
        },
    )
    framework_body = UpdateAgentRequest(**core)
    view: TeamAgentView = await _core_update_agent(
        agent_id=agent_id,
        body=framework_body,
        user_id=user_id,
        storage=storage,
        access=access,
    )
    # 记忆字段：None 不改；传值则更新
    mem_updates = {
        k: v
        for k, v in {
            "memory_update_prompt": body.memory_update_prompt,
            "memory_enabled": body.memory_enabled,
            "memory_type": body.memory_type,
            "memory_update_rounds": body.memory_update_rounds,
        }.items()
        if v is not None
    }
    if mem_updates:
        existing = await memory_config.memory_get(user_id, agent_id)
        merged = (
            existing.model_copy(update=mem_updates)
            if existing
            else memory_config.MemoryConfig(**mem_updates)
        )
        try:
            await memory_config.memory_upsert(user_id, agent_id, merged)
        except Exception:
            logger.exception("memory upsert failed for %s", agent_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Agent updated but memory config write failed.",
            )
    final_mem = await memory_config.memory_get(user_id, agent_id)
    mem_fields = (
        final_mem.model_dump() if final_mem is not None else _memory_defaults()
    )
    return AgentViewWithMemory.model_validate(
        {**view.model_dump(), **mem_fields},
    )


@agent_api_router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an agent (cascades memory config)",
)
async def delete_agent_with_memory(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    session_service=Depends(get_session_service),
    access=Depends(get_resource_access_service),
) -> Response:
    """删除智能体：框架原逻辑（404/403 语义）+ 侧边记忆记录清理。"""
    await _core_delete_agent(
        agent_id=agent_id,
        user_id=user_id,
        session_service=session_service,
        access=access,
    )
    try:
        await memory_config.memory_delete(user_id, agent_id)
    except Exception:
        logger.exception("memory delete failed for %s; agent already deleted", agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------
# 路由接管装配
# ------------------------------------------------------------------

_SHADOWED_PATHS = {"/agent/", "/agent/{agent_id}"}
_SHADOWED_METHODS = {"GET", "POST", "PATCH", "DELETE"}


def _is_shadowed(route) -> bool:
    """判断一条路由是否被包裹层覆盖（路径 + HTTP 方法重叠）。"""
    return route.path in _SHADOWED_PATHS and any(
        m in _SHADOWED_METHODS for m in (route.methods or set())
    )


def install_agent_memory_router(app) -> None:
    """把包裹路由前插到 app，并移除框架被覆盖的 4 条路由。

    Starlette 按 ``app.router.routes`` 列表顺序匹配，前插即覆盖同名
    路径；移除框架路由保证 OpenAPI 无重复条目、行为单一来源。
    非覆盖路径（/agent/schema/v2、/agent/{id}/team/* 等）不受影响。

    兼容两种 ``app.router.routes`` 形态：
    - 顶层展开的 ``APIRoute``（旧 Starlette）：直接过滤掉被覆盖路由；
    - ``_IncludedRouter`` 懒加载包装（Starlette 1.6+，当前实际环境）：
      ``include_router`` 不再把路由展开进 ``app.router.routes``，而是
      用一个持有 ``original_router`` 的包装对象；需从对应的
      ``original_router.routes`` 中移除被覆盖路由，OpenAPI 才能去重。
    """
    kept: list = []
    for route in app.router.routes:
        if isinstance(route, APIRoute) and _is_shadowed(route):
            continue
        kept.append(route)
    # Starlette 1.6+：处理 _IncludedRouter（懒加载）内的被覆盖路由
    for route in app.router.routes:
        orig = getattr(route, "original_router", None)
        if orig is None:
            continue
        orig_routes = getattr(orig, "routes", [])
        if any(
            isinstance(rt, APIRoute) and _is_shadowed(rt) for rt in orig_routes
        ):
            orig.routes = [
                rt for rt in orig_routes if not (
                    isinstance(rt, APIRoute) and _is_shadowed(rt)
                )
            ]
    app.router.routes = [*agent_api_router.routes, *kept]


__all__ = [
    "agent_api_router",
    "install_agent_memory_router",
    "CreateAgentRequestWithMemory",
    "UpdateAgentRequestWithMemory",
    "CreateAgentResponseWithMemory",
    "AgentViewWithMemory",
    "ListAgentsResponseWithMemory",
]
