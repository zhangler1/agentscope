# -*- coding: utf-8 -*-
"""智能体模板名单管理 API —— ``agent_template`` 表的增删改查。

端点（本 router 自带 ``/agent/template`` 前缀，框架随后统一加 ``/api``）::

    GET    /api/agent/template              列出模板（可按分类/启停过滤）
    GET    /api/agent/template/categories   列出已有分类（供前端筛选下拉）
    GET    /api/agent/template/agents       按名单返回智能体明细（支持
                                            ``category`` 过滤 + 分页）
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
from pydantic import BaseModel, Field, field_validator

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
    list_template_categories,
    list_template_entries,
    normalize_skills,
    update_template_entry,
)
from bocomadp.team_store import list_teams

logger = logging.getLogger("bocomadp.routers.agent_template")

agent_template_router = APIRouter(
    prefix="/agent/template",
    tags=["agent-template"],
)

#: ``skills`` 元素必须是 ``namespace:name`` 形式（与 ``skill_router`` 的
#: full name 口径一致，如 ``global:rollback-check-sql``）——这样复制时可以直接
#: 拿去 hub 下载安装，不用再猜 namespace。
_SKILL_SEP = ":"
#: 单个技能名长度上限（与 ``agent_template.skills`` 里存的字符串惯例一致）。
_SKILL_NAME_MAX_LEN = 128
#: 单个模板最多登记多少个技能。
_SKILLS_MAX_COUNT = 50


def _clean_skills(values: list[str] | None) -> list[str]:
    """校验并规范化技能清单（元素必须为 ``namespace:name``）。

    规范化（strip / 去空 / 去重保序）在 store 里也会做一遍，这里做是为了
    **尽早报 422**（前端能拿到具体哪个元素不合法），而不是静默存脏数据。
    """
    cleaned = normalize_skills(values)
    if len(cleaned) > _SKILLS_MAX_COUNT:
        raise ValueError(
            f"skills 最多 {_SKILLS_MAX_COUNT} 项，当前 {len(cleaned)} 项。",
        )
    for name in cleaned:
        if len(name) > _SKILL_NAME_MAX_LEN:
            raise ValueError(
                f"技能名最长 {_SKILL_NAME_MAX_LEN} 字符：{name!r}。",
            )
        namespace, sep, skill = name.partition(_SKILL_SEP)
        if not sep or not namespace or not skill:
            raise ValueError(
                f"技能名必须是 'namespace:name' 形式（如 "
                f"'global:rollback-check-sql'），收到 {name!r}。",
            )
    return cleaned


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class AgentTemplateCreateRequest(BaseModel):
    """``POST /agent/template`` 请求体。"""

    agent_id: str = Field(description="要加入模板名单的智能体 id。")
    owner_user_id: str | None = Field(
        default=None,
        description=(
            "智能体归属用户，**原样存储，不做任何校验**（可任意填写，也"
            "可以是不存在的用户）；缺省（``null`` / 空串）存空串。"
        ),
    )
    title: str = Field(
        default="",
        description="模板展示名；空串 = 使用智能体本名。",
    )
    description: str = Field(default="", description="模板说明（一句话）。")
    category: str = Field(default="", description="分类，前端按类目分组。")
    skills: list[str] = Field(
        default_factory=list,
        description=(
            "期望安装的技能清单，元素为 ``namespace:name``（如 "
            "``global:rollback-check-sql``）；缺省空数组 = 不带技能。"
            f"最多 {_SKILLS_MAX_COUNT} 项，自动去重（保序）。"
        ),
    )
    sort_order: int = Field(default=0, description="展示顺序，小者在前。")
    enabled: bool = Field(default=True, description="是否允许被复制。")

    @field_validator("skills")
    @classmethod
    def _validate_skills(cls, value: list[str]) -> list[str]:
        return _clean_skills(value)


class AgentTemplateUpdateRequest(BaseModel):
    """``PUT /agent/template/{agent_id}`` 请求体。

    PATCH 语义：**只有显式传入的字段会被修改**；显式传 ``null`` 等同
    不改（避免把列写成 NULL），清空请传空串。``skills`` 例外：
    ``null`` = 不改，**清空要传 ``[]``**。
    """

    owner_user_id: str | None = None
    title: str | None = None
    description: str | None = None
    category: str | None = None
    skills: list[str] | None = Field(
        default=None,
        description=(
            "整体替换技能清单（元素 ``namespace:name``）；传 ``[]`` 清空，"
            "不传 / 传 ``null`` 保持不变。"
        ),
    )
    sort_order: int | None = None
    enabled: bool | None = None

    @field_validator("skills")
    @classmethod
    def _validate_skills(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_skills(value)


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
    - ``skills``：模板表上的技能清单（元素 ``namespace:name``）；
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
    skills: list[str] = Field(
        default_factory=list,
        description=(
            "模板登记的技能清单（``agent_template.skills``，元素 "
            "``namespace:name``）；供前端展示该模板会带哪些技能。"
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


class AgentTemplateCategoriesResponse(BaseModel):
    """``GET /agent/template/categories`` 响应体。"""

    categories: list[str] = Field(
        default_factory=list,
        description=(
            "模板名单里出现过的分类（去重、字典序升序、**不含空分类**）。"
            "可直接用作 ``GET /agent/template/agents?category=`` 的候选值。"
        ),
    )
    total: int = Field(default=0, description="分类数量（``len(categories)``）。")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


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


async def _resolve_agent_record(
    storage: Any,
    agent_id: str,
) -> Any | None:
    """**只按 ``agent_id`` 查智能体记录**（跨 owner 全局查）。

    模板行上的 ``owner_user_id`` 不参与定位、也不做校验（可空 / 可任意填，
    它只是登记信息），所以这里直接用 :func:`bocomadp.open_agent_access.
    get_agent_global` 按 id 查：

    - **仅 SQL 主存储**支持跨 owner 查询（``storage._session()``）；Redis 等
      按 user 分片的主存储没有全局索引 → 返回 ``None``，该模板行按
      "智能体不存在"跳过；
    - 查不到 = 智能体真的不存在（不是"owner 填错"）。

    Args:
        storage (`Any`):
            框架 storage。
        agent_id (`str`):
            智能体 id（``agents`` 表主键，全局唯一）。

    Returns:
        `Any | None`:
        :class:`AgentRecord`；不存在或主存储不支持跨 owner 查询时为 ``None``。
    """
    try:
        from bocomadp.open_agent_access import get_agent_global
    except Exception:  # noqa: BLE001 —— 查询能力不可用时按"不存在"处理
        logger.warning(
            "agent_template: global agent lookup unavailable",
            exc_info=True,
        )
        return None
    return await get_agent_global(storage, agent_id)


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
    ``sort_order ASC, created_at DESC, agent_id ASC`` 分页，再逐个取智能体
    记录，返回     ``{id, user_id, source, data, created_at, updated_at, category, skills,
    editable, is_team, parent_agent_id, is_self_built}``：

    - **定位智能体记录只看 ``agent_id``**（跨 owner 全局查，详见
      :func:`_resolve_agent_record`）：模板行上的 ``owner_user_id`` 不参与
      定位、也不校验，留空 / 乱填都能正常返回明细（它只是登记信息）；
    - ``data`` 经 :class:`~agentscope.app.storage.AgentData` 序列化，
      库里老行 payload 缺键（如 ``description``）会自动补模型默认值，
      与 ``GET /api/agent/`` 的 ``data`` 形状保持一致；
    - ``category`` / ``skills`` 取自模板行（``agent_template``），供前端
      按类目分组、展示该模板会带哪些技能；其余模板字段（``title`` /
      ``sort_order`` / ``enabled``）仍只用于过滤与排序，不返回；
    - ``editable`` / ``is_team`` / ``parent_agent_id`` / ``is_self_built``
      **与 ``GET /api/agent/`` 同源、按调用者现算**，不是常量：
      ``editable`` 取访问层 ``list_resource``（顶层）给出的 viewer-relative
      布尔值（同一次请求复用同一份映射）；团队三项由 ``expert_team_relations``
      表按**智能体真实归属**（``record.user_id``）推导，判定逻辑与
      ``routers/agent.py::_to_team_view`` 一致。调用者列表里看不到的智能体
      （平台模板归属 ``default``）→ ``editable=False``；
    - 模板行指向的智能体**真的不存在**时跳过该条并打 warning，``total``
      仍按模板表计数（因此 ``len(agents)`` 可能小于 ``pageSize``）。

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
        # 只按 agent_id 查（跨 owner）：模板行上的 owner_user_id 不参与定位、
        # 也不做校验，留空 / 乱填都能正常返回明细。
        record = await _resolve_agent_record(storage, entry.agent_id)
        if record is None:
            logger.warning(
                "agent_template: template row points to a missing agent: "
                "agent_id=%s owner=%s",
                entry.agent_id,
                entry.owner_user_id,
            )
            continue
        # 团队档案按**智能体真实归属**（record.user_id）查，而不是模板行上
        # 登记的 owner —— 后者可能空/错，用它查会漏掉团长/成员标记。
        is_team, parent_agent_id, is_self_built = _team_markers(
            await _teams_of_owner(storage, record.user_id, teams_cache),
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
                skills=list(entry.skills or []),
                editable=editable_map.get(record.id, False),
                is_team=is_team,
                parent_agent_id=parent_agent_id,
                is_self_built=is_self_built,
            ),
        )
    return TemplateAgentsResponse(agents=agents, total=total)


@agent_template_router.get(
    "/categories",
    response_model=AgentTemplateCategoriesResponse,
    summary="列出模板已有分类（供前端筛选）",
)
async def list_agent_template_categories(
    enabled: bool | None = Query(
        default=None,
        description="按模板启停过滤；缺省统计全部（含已下架）。",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> AgentTemplateCategoriesResponse:
    """列出 ``agent_template`` 里出现过的分类，供前端渲染分类筛选。

    与 ``GET /agent/template/agents`` 的 ``category`` 参数同源（同一列），
    因此返回的每个值都能直接拿去过滤；分类去重后按字典序升序返回。
    **空分类不返回**（新增模板时未填 ``category`` 的行），这类模板在
    列表端点里表现为"不传 ``category`` 时能看到、传任意分类时看不到"。

    Args:
        enabled (`bool | None`):
            可选启停过滤：``true`` 只统计上架模板、``false`` 只统计已下架，
            缺省统计全部。口径与列表端点一致。
        user_id (`str`):
            Injected authenticated user ID（运营/只读接口，仅要求身份）。
        storage (`StorageBase`):
            Injected storage backend.

    Returns:
        `AgentTemplateCategoriesResponse`:
            ``{"categories": [...], "total": n}``。
    """
    categories = await list_template_categories(storage, enabled=enabled)
    return AgentTemplateCategoriesResponse(
        categories=categories,
        total=len(categories),
    )


# 注意：与 ``/agents`` 同理，本端点必须声明在 ``GET /{agent_id}`` **之前**，
# 否则 ``categories`` 会被 ``/{agent_id}`` 抢先匹配成 agent_id 并 404。
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

    **不做任何校验**：``agent_id`` 不要求出现在 ``agents`` 表里，
    ``owner_user_id`` 原样存储（可任意填写、可以是空串 / 不存在的用户）
    —— 便于先占位登记、再由运营补真实归属。唯一校验是"已在名单内 →
    **409**"。

    ``skills`` 会做**格式校验**（元素必须是 ``namespace:name``，如
    ``global:rollback-check-sql``；最多 50 项、单名 ≤128 字符），不合法
    直接 422；合法值会被规范化（strip / 去空 / 去重保序）后入库，但
    **不校验技能是否真实存在**（技能在外部 hub，允许先登记后安装）。

    ``owner_user_id`` 只是**登记信息**，不参与任何查询：明细端点
    ``GET /agent/template/agents`` 只按 ``agent_id`` 跨 owner 取记录
    （详见 :func:`_resolve_agent_record`），所以它填什么都不会影响该模板
    能否被查出来。

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
        `HTTPException`: 409 已在名单内。
    """
    owner = (body.owner_user_id or "").strip()

    entry = AgentTemplateEntry(
        agent_id=body.agent_id,
        owner_user_id=owner,
        title=body.title,
        description=body.description,
        category=body.category,
        skills=body.skills,
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
    ``{"title": "..."}`` 改展示名、
    ``{"skills": ["global:rollback-check-sql"]}`` 整体替换技能清单。

    ``skills`` 语义：传值 = **整体替换**（同样校验 ``namespace:name``）；
    传 ``[]`` = 清空；不传 / 传 ``null`` = 保持不变。
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
