# -*- coding: utf-8 -*-
"""智能体模板名单管理 API —— ``agent_template`` 表的增删改查。

端点（本 router 自带 ``/agent/template`` 前缀，框架随后统一加 ``/api``）::

    GET    /api/agent/template              列出模板（可按分类/启停过滤）
    GET    /api/agent/template/{agent_id}   查询单条模板
    POST   /api/agent/template              新增模板（加入可复制名单）
    PUT    /api/agent/template/{agent_id}   部分更新模板
    DELETE /api/agent/template/{agent_id}   移出模板名单

语义：**只有名单内且 ``enabled=true`` 的智能体才允许被复制**（由
``POST /api/agent/{agent_id}/copy`` 校验）。本表与 ``agent_market``
（是否上架）互不影响，同一智能体可同时在两张表里。

权限：与市场管理接口保持同一口径 —— 带 ``X-User-ID`` 即可调用（运营
接口）；生产环境若要收敛，在 :func:`_require_operator` 里加白名单校验。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from agentscope.app._service import ResourceAccessService
from agentscope.app.access import ResourceKind
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_storage,
)
from agentscope.app.storage import AgentData, StorageBase

from bocomadp.agent_template_store import (
    AgentTemplateEntry,
    create_template_entry,
    delete_template_entry,
    get_template_entry,
    list_template_entries,
    update_template_entry,
)
from bocomadp.team_store import list_teams

logger = logging.getLogger("bocomadp.routers.agent_template")

agent_template_router = APIRouter(
    prefix="/agent/template",
    tags=["agent-template"],
)


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class AgentTemplateCreateRequest(BaseModel):
    """``POST /agent/template`` 请求体。"""

    agent_id: str = Field(description="要加入模板名单的智能体 id。")
    owner_user_id: str | None = Field(
        default=None,
        description=(
            "智能体归属用户（模板通常归属 ``default``）；缺省时由后端"
            "跨 owner 探测，探测不到视为智能体不存在。"
        ),
    )
    title: str = Field(
        default="",
        description="模板展示名；空串 = 使用智能体本名。",
    )
    description: str = Field(default="", description="模板说明（一句话）。")
    category: str = Field(default="", description="分类，前端按类目分组。")
    sort_order: int = Field(default=0, description="展示顺序，小者在前。")
    enabled: bool = Field(default=True, description="是否允许被复制。")


class AgentTemplateUpdateRequest(BaseModel):
    """``PUT /agent/template/{agent_id}`` 请求体。

    PATCH 语义：**只有显式传入的字段会被修改**；显式传 ``null`` 等同
    不改（避免把列写成 NULL），清空请传空串。
    """

    owner_user_id: str | None = None
    title: str | None = None
    description: str | None = None
    category: str | None = None
    sort_order: int | None = None
    enabled: bool | None = None


class AgentTemplateListResponse(BaseModel):
    """``GET /agent/template`` 响应体。"""

    templates: list[AgentTemplateEntry] = Field(default_factory=list)
    total: int = Field(default=0, description="本次返回条数（未分页）。")


class AgentTemplateDeleteResponse(BaseModel):
    """``DELETE /agent/template/{agent_id}`` 响应体。"""

    agent_id: str
    deleted: bool = True


class TemplateAgentItem(BaseModel):
    """模板智能体的明细视图。

    在智能体本体与归属信息之外，额外带上：

    - ``category``：模板表（``agent_template``）上的分类，前端按类目分组；
    - ``editable`` / ``is_team`` / ``parent_agent_id`` / ``is_self_built``：
      与 ``GET /api/agent/`` 的 :class:`TeamAgentView` 字段对齐，且**取值
      同源**（都是现查表/权限算出来的，不是常量）：
      ``editable`` 来自访问层（对当前 ``X-User-ID`` 而言能否 PATCH/DELETE），
      团队三项来自 ``expert_team_relations`` 表。
    """

    id: str = Field(description="智能体 id（``agents`` 表主键）。")
    user_id: str = Field(description="智能体归属用户。")
    source: str = Field(description="来源：``user`` / ``team``。")
    data: AgentData = Field(
        description=(
            "智能体本体（``agents.payload.data``）；经 ``AgentData`` "
            "模型序列化，库里老行缺键时自动补默认值。"
        ),
    )
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最后更新时间。")
    category: str = Field(
        default="",
        description=(
            "模板分类（``agent_template.category``）；仅作展示分组，"
            "与 ``GET /agent/template`` 的过滤参数同名。"
        ),
    )
    editable: bool = Field(
        default=False,
        description=(
            "当前调用者能否 PATCH/DELETE 该智能体（viewer-relative）。"
            "与 ``GET /api/agent/`` 同源：取访问层 ``list_resource`` 给出的"
            " ``editable``；调用者列表里看不到的智能体（如 ``default`` 归属的"
            "平台模板）为 ``false``。"
        ),
    )
    is_team: bool = Field(
        default=False,
        description=(
            "是否专家团团长：``expert_team_relations`` 里存在 "
            "``leader_agent_id`` = 该智能体的团队档案。"
        ),
    )
    parent_agent_id: str | None = Field(
        default=None,
        description=(
            "作为自建成员挂靠的团长 id；不是团队成员为 ``null``。"
        ),
    )
    is_self_built: bool | None = Field(
        default=None,
        description=(
            "是否某团长的自建成员；不是团队成员为 ``null``"
            "（与 ``routers/agent.py::_to_team_view`` 的判定一致）。"
        ),
    )


class TemplateAgentsResponse(BaseModel):
    """``GET /agent/template/agents`` 响应体。"""

    agents: list[TemplateAgentItem] = Field(default_factory=list)
    total: int = Field(default=0, description="模板总数（分页前）。")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


async def _detect_owner(storage: Any, agent_id: str) -> str:
    """跨 owner 探测智能体归属（SQL 主存储）；探测不到返回空串。

    列表/查询接口走 ``storage`` 的 owner 作用域，而模板通常归属
    ``default``、调用方不是 owner，因此加入名单时按 id 全局查一次
    ``agents`` 表拿真实 owner（复用开放访问模块的现成实现）。
    """
    try:
        from bocomadp.open_agent_access import get_agent_global
    except Exception:  # noqa: BLE001 —— 探测能力不可用时由调用方回退 404
        logger.warning("owner detection unavailable", exc_info=True)
        return ""
    record = await get_agent_global(storage, agent_id)
    if record is None:
        return ""
    return str(getattr(record, "user_id", "") or "")


def _team_markers(
    teams: list[Any],
    agent_id: str,
) -> tuple[bool, str | None, bool | None]:
    """从团队档案推导 ``(is_team, parent_agent_id, is_self_built)``。

    判定与 ``routers/agent.py::_to_team_view`` **逐条一致**：某团档案的
    ``leader_agent_id`` 命中 → ``is_team=True``；出现在某团名册里且是
    ``self_built`` → 回填团长 id 与 ``True``；两者都不命中 →
    ``(False, None, None)``（``is_self_built`` 保持三态里的 ``null``，
    与顶层 ``GET /api/agent/`` 的口径相同）。

    Args:
        teams (`list[Any]`):
            某 owner 名下的全部团队档案（``team_store.list_teams``）。
        agent_id (`str`):
            待判定的智能体 id。
    """
    is_team = False
    parent_agent_id: str | None = None
    is_self_built: bool | None = None
    for team in teams:
        if team.leader_agent_id == agent_id:
            is_team = True
        if team.is_self_built(agent_id):
            parent_agent_id = team.leader_agent_id
            is_self_built = True
    return is_team, parent_agent_id, is_self_built


async def _editable_by_agent(
    access: Any,
    user_id: str,
) -> dict[str, bool]:
    """``GET /api/agent/`` 同源的 "可编辑" 映射：``agent_id -> editable``。

    直接用访问层的 ``list_resource``（顶层，不传 ``parent_agent_id``），
    拿到的每个 :class:`AgentView` 都带 viewer-relative 的 ``editable``，
    与 ``GET /api/agent/`` 返回的那个布尔值**是同一个来源**，所以两边
    永远一致（含"共享给调用者但只读"→ ``False`` 这类情况）。

    列表里没出现的智能体（例如归属 ``default`` 的平台模板对普通用户
    不可见）在调用方视角下本就不可编辑，统一按 ``False`` 处理。

    Args:
        access (`Any`):
            ``ResourceAccessService`` 实例。
        user_id (`str`):
            调用者（``X-User-ID``）。
    """
    try:
        views = await access.list_resource(user_id, ResourceKind.AGENT)
    except Exception:  # noqa: BLE001 —— 取不到可见性时不阻断列表
        logger.warning(
            "agent_template: list_resource failed; editable defaults to false",
            exc_info=True,
        )
        return {}
    return {v.id: bool(getattr(v, "editable", False)) for v in views}


async def _teams_of_owner(
    storage: Any,
    owner_id: str,
    cache: dict[str, list[Any]],
) -> list[Any]:
    """按 owner 取团队档案（同一次请求内按 owner 缓存，避免逐条查库）。"""
    if owner_id not in cache:
        cache[owner_id] = await list_teams(storage, owner_id)
    return cache[owner_id]


# ---------------------------------------------------------------------------
# 增删改查
# ---------------------------------------------------------------------------


@agent_template_router.get(
    "",
    response_model=AgentTemplateListResponse,
    summary="列出模板名单",
)
async def list_agent_templates(
    category: str | None = Query(
        default=None,
        description="按分类精确过滤；缺省不过滤。",
    ),
    enabled: bool | None = Query(
        default=None,
        description="按启停过滤；缺省返回全部（含已下架）。",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateListResponse:
    """列出模板，按 ``sort_order ASC, created_at DESC, agent_id ASC`` 排序。

    Args:
        category (`str | None`):
            可选分类过滤。
        enabled (`bool | None`):
            可选启停过滤。
        user_id (`str`):
            Injected authenticated user ID（运营接口仅要求身份）。
        storage (`StorageBase`):
            Injected storage backend.

    Returns:
        `AgentTemplateListResponse`:
            ``{templates: [...], total: n}``。
    """
    entries = await list_template_entries(
        storage,
        category=category,
        enabled=enabled,
    )
    return AgentTemplateListResponse(
        templates=entries,
        total=len(entries),
    )


# 注意：本端点必须声明在 ``GET /{agent_id}`` **之前**，否则 ``agents``
# 会被 ``/{agent_id}`` 抢先匹配成 agent_id。
@agent_template_router.get(
    "/agents",
    response_model=TemplateAgentsResponse,
    summary="按模板名单返回智能体明细（agents 表 payload）",
)
async def list_template_agents(
    category: str | None = Query(
        default=None,
        description="按模板分类过滤；缺省不过滤。",
    ),
    enabled: bool | None = Query(
        default=None,
        description="按模板启停过滤；缺省返回全部（含已下架）。",
    ),
    page_num: int = Query(
        default=1,
        ge=1,
        alias="pageNum",
        description="页码，1 起。",
    ),
    page_size: int = Query(
        default=10,
        ge=1,
        le=100,
        alias="pageSize",
        description="每页条数，1-100。",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> TemplateAgentsResponse:
    """按 ``agent_template`` 名单批量返回对应智能体的完整信息。

    流程：先取模板名单（含已下架，除非按 ``enabled`` 过滤），按模板表顺序
    ``sort_order ASC, created_at DESC, agent_id ASC`` 分页，再逐个用模板行的
    ``owner_user_id`` 以 owner-scoped 方式读 ``agents`` 表，返回
    ``{id, user_id, source, data, created_at, updated_at, category,
    editable, is_team, parent_agent_id, is_self_built}``：

    - ``data`` 经 :class:`~agentscope.app.storage.AgentData` 序列化，
      库里老行 payload 缺键（如 ``description``）会自动补模型默认值，
      与 ``GET /api/agent/`` 的 ``data`` 形状保持一致；
    - ``category`` 取自模板行（``agent_template.category``），供前端按类目
      分组；其余模板字段（``title`` / ``sort_order`` / ``enabled``）仍只用于
      过滤与排序，不返回；
    - ``editable`` / ``is_team`` / ``parent_agent_id`` / ``is_self_built``
      **与 ``GET /api/agent/`` 同源、按调用者现算**，不是常量：
      ``editable`` 取访问层 ``list_resource``（顶层）给出的 viewer-relative
      布尔值（同一次请求复用同一份映射）；团队三项由 ``expert_team_relations``
      表按 ``owner_user_id`` 推导，判定逻辑与 ``routers/agent.py::_to_team_view``
      一致。调用者列表里看不到的智能体（平台模板归属 ``default``）→
      ``editable=False``；
    - 模板行是孤儿（``agents`` 表已无该 id）时跳过该条并打 warning，
      ``total`` 仍按模板表计数（因此 ``len(agents)`` 可能小于 ``pageSize``）。

    Args:
        category (`str | None`):
            可选模板分类过滤。
        enabled (`bool | None`):
            可选模板启停过滤。
        page_num (`int`):
            页码（query 名 ``pageNum``）。
        page_size (`int`):
            每页条数（query 名 ``pageSize``）。
        user_id (`str`):
            Injected authenticated user ID（用于计算 ``editable``）。
        storage (`StorageBase`):
            Injected storage backend。
        access (`ResourceAccessService`):
            Injected access service —— ``editable`` 与 ``GET /api/agent/``
            同源，避免两处口径漂移。

    Returns:
        `TemplateAgentsResponse`:
            ``{"agents": [...], "total": n}``。
    """
    entries = await list_template_entries(
        storage,
        category=category,
        enabled=enabled,
    )
    total = len(entries)
    start = (page_num - 1) * page_size
    page = entries[start : start + page_size]

    # editable：一次 list_resource 拿全量，来源与 GET /api/agent/ 相同。
    editable_map = await _editable_by_agent(access, user_id)
    # 团队档案：按 owner 缓存（同一次请求里同一 owner 只查一次库）。
    teams_cache: dict[str, list[Any]] = {}

    agents: list[TemplateAgentItem] = []
    for entry in page:
        record = await storage.get_agent(
            entry.owner_user_id,
            entry.agent_id,
        )
        if record is None:
            logger.warning(
                "agent_template: orphan row skipped: agent_id=%s owner=%s",
                entry.agent_id,
                entry.owner_user_id,
            )
            continue
        is_team, parent_agent_id, is_self_built = _team_markers(
            await _teams_of_owner(storage, entry.owner_user_id, teams_cache),
            record.id,
        )
        agents.append(
            TemplateAgentItem(
                id=record.id,
                user_id=record.user_id,
                source=str(record.source),
                data=record.data,
                created_at=record.created_at,
                updated_at=record.updated_at,
                category=entry.category,
                editable=editable_map.get(record.id, False),
                is_team=is_team,
                parent_agent_id=parent_agent_id,
                is_self_built=is_self_built,
            ),
        )
    return TemplateAgentsResponse(agents=agents, total=total)


@agent_template_router.get(
    "/{agent_id}",
    response_model=AgentTemplateEntry,
    summary="查询单个智能体的模板记录",
)
async def get_agent_template(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateEntry:
    """读一条模板记录；不在名单内 → 404。"""
    entry = await get_template_entry(storage, agent_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} is not in the template list.",
        )
    return entry


@agent_template_router.post(
    "",
    response_model=AgentTemplateEntry,
    status_code=status.HTTP_201_CREATED,
    summary="新增模板（加入可复制名单）",
)
async def create_agent_template(
    body: AgentTemplateCreateRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateEntry:
    """把一个智能体加入模板名单。

    校验顺序：``owner_user_id`` 缺省时先跨 owner 探测（探测不到 →
    **404** 智能体不存在）→ 已在名单内 → **409**。

    Args:
        body (`AgentTemplateCreateRequest`):
            名单条目（``agent_id`` 必填，其余可选）。
        user_id (`str`):
            Injected authenticated user ID。
        storage (`StorageBase`):
            Injected storage backend.

    Returns:
        `AgentTemplateEntry`:
            落库后的条目（含 ``created_at`` / ``updated_at``）。

    Raises:
        `HTTPException`: 404 智能体不存在；409 已在名单内。
    """
    owner = (body.owner_user_id or "").strip()
    if not owner:
        owner = await _detect_owner(storage, body.agent_id)
        if not owner:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent {body.agent_id!r} not found.",
            )

    entry = AgentTemplateEntry(
        agent_id=body.agent_id,
        owner_user_id=owner,
        title=body.title,
        description=body.description,
        category=body.category,
        sort_order=body.sort_order,
        enabled=body.enabled,
    )
    created = await create_template_entry(storage, entry)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {body.agent_id!r} is already a template.",
        )
    stored = await get_template_entry(storage, body.agent_id)
    if stored is None:  # 理论不可达：刚插入的行读不到
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Template entry was not persisted.",
        )
    logger.info(
        "agent_template: added %s (owner=%s, category=%r, enabled=%s) by %s",
        body.agent_id,
        owner,
        stored.category,
        stored.enabled,
        user_id,
    )
    return stored


@agent_template_router.put(
    "/{agent_id}",
    response_model=AgentTemplateEntry,
    summary="更新模板（部分字段）",
)
async def update_agent_template(
    agent_id: str,
    body: AgentTemplateUpdateRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateEntry:
    """部分更新模板记录（PATCH 语义）；不在名单内 → 404。

    常用场景：``{"enabled": false}`` 临时下架、``{"sort_order": 5}`` 置顶、
    ``{"title": "..."}`` 改展示名。
    """
    patch = body.model_dump(exclude_unset=True)
    updated = await update_template_entry(storage, agent_id, patch)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} is not in the template list.",
        )
    logger.info(
        "agent_template: updated %s %s by %s",
        agent_id,
        sorted(patch.keys()),
        user_id,
    )
    return updated


@agent_template_router.delete(
    "/{agent_id}",
    response_model=AgentTemplateDeleteResponse,
    summary="移出模板名单",
)
async def delete_agent_template(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateDeleteResponse:
    """把智能体移出模板名单（之后不可再被复制）；不在名单内 → 404。"""
    deleted = await delete_template_entry(storage, agent_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {agent_id!r} is not in the template list.",
        )
    logger.info("agent_template: removed %s by %s", agent_id, user_id)
    return AgentTemplateDeleteResponse(agent_id=agent_id, deleted=True)


__all__ = ["agent_template_router"]
