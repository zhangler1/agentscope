# -*- coding: utf-8 -*-
"""BocomADP agent-market router — 市场查询 + 精选推荐 + 上架管理 + 标签管理。

市场名单完全由 ``agent_market`` 表的行决定（**有行 = 在市场**）：
``agents`` 表是全量智能体，``user_id`` 不再承载"平台"语义。名单来源：

- 平台内置智能体：运营手动 INSERT（``agent_id`` 填 ``agents.id``）；
- 个人智能体：owner 调 publish 插行、unpublish 删行。

标签即 ``agent_market.tag`` 自由字符串（≤64 字符）：打标内容原样存储，
无预设清单；空串 = 未打标。打标/撕标**仅限名单内智能体**（不在名单
404，打标不允许隐式上架），权限全开放（带 X-User-ID 即可调）。

端点（统一挂 ``/agent/market`` 前缀，main.py 再统一加 ``/api``）：

查询类（无权限要求）：
- ``GET  /agent/market``              市场列表（名单内全部；按
                                       updated_at 倒序分页，可按 tag 筛选）；
- ``GET  /agent/market/featured``     精选推荐：按实时热度（sessions 会话数）
                                       倒序取前 N，同分按 updated_at 倒序兜底。

上架管理（仅智能体 owner）：
- ``POST /agent/market/{agent_id}/publish``    发布：往名单插一行；
- ``POST /agent/market/{agent_id}/unpublish``  撤回：删行（204，幂等，
                                                标签随行消失）。

标签管理（全开放，仅名单内智能体）：
- ``PUT  /agent/market/{agent_id}``   打标（tag 非空）或撕标（tag 空串）；
- ``DELETE /agent/market/{agent_id}`` 撕标：tag 置空（204，幂等）。

设计要点：

- **查询实现**：两步小查询——先查 ``agent_market`` 拿全部名单 id，
  再查 ``agents`` 表取本体（``id IN 名单``，取 name/source/时间戳），
  风格与 ``_tag_map`` / 热度聚合的分步小查询一致。
- **不做查询过滤**：名单完全由运营手动维护 + owner 发布构成，
  放进去什么就展示什么（系统内置 ``_`` 开头智能体是代码硬编码创建
  的内部工具，运营手动维护名单时不放进即可）。
- **热度（实时聚合）**：heat 不落库，每次查询时对 ``sessions``
  表按 ``agent_id`` GROUP BY 计数。**按市场智能体集合过滤，而不是按
  会话的 user_id 过滤**——sessions.user_id 记的是使用者，任何人对
  市场内智能体的使用都计入该智能体热度。将来量大再升级为固化分值
  + 定时增量累加，接口签名不变。
- **操作留痕只打控制台日志**：上架/标签操作调用
  :func:`bocomadp.market_audit.log_audit`，以 ``[AGENT_MARKET_AUDIT]``
  前缀打印 INFO，用 ``docker compose logs -f agentscope-service``
  直接查看，不再落库（将来要长期留痕只需替换该模块的日志实现）。
- 框架的 ``AgentRow`` / ``SessionRow`` 是私有模块，但 bocomadp 层
  访问框架私有成员（``_session_factory``）已有 ``team_store.py`` 先例，
  此处一致：只读不写框架表。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

logger = logging.getLogger("bocomadp.market_router")

from agentscope.app.deps import get_current_user_id, get_storage
from agentscope.app.storage import StorageBase
from agentscope.app.storage._sql._tables import AgentRow, SessionRow

from bocomadp.market_audit import log_audit
from bocomadp.market_store import (
    delete_market_entry,
    get_market_entry,
    insert_market_entry,
    list_market_entries,
    list_market_tags,
    set_market_tag,
)
from bocomadp.routers._schema.market import (
    MarketAgentView,
    MarketEntryView,
    MarketListResponse,
    MarketUpsertRequest,
)

market_router = APIRouter(
    prefix="/agent/market",
    tags=["agent-market"],
    responses={404: {"description": "Not found"}},
)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

# tag 列宽（与 market_store.AgentMarketRow.tag 一致）
_TAG_MAX_LEN = 64


async def _find_agent_row(
    storage: StorageBase,
    agent_id: str,
) -> AgentRow | None:
    """按 id 查智能体（不限定归属者）——打标/上架的存在性校验用。"""
    factory = getattr(storage, "_session_factory", None)
    if factory is None:
        return None
    async with factory() as session:
        return (
            await session.execute(
                select(AgentRow).where(AgentRow.id == agent_id),
            )
        ).scalars().first()


async def _market_agent_rows(storage: StorageBase) -> list[AgentRow]:
    """市场可见的 agents 行（updated_at 倒序），范围 = agent_market 名单。

    两步小查询：先拿 ``agent_market`` 全部名单 id，再按 ``id IN 名单``
    查 agents 本体。**不做防御性过滤**——名单完全由运营手动维护 +
    owner 发布构成，放进去什么就展示什么。
    返回完整 ORM 行（名称在 ``payload["data"]["name"]``，
    框架 mapper 的存储契约：payload = record.model_dump 去掉
    id/created_at/updated_at/user_id/source 后的剩余部分，即
    ``{"data": {...AgentData...}}``）。
    """
    factory = getattr(storage, "_session_factory", None)
    if factory is None:
        return []
    market_ids = {
        e.agent_id for e in await list_market_entries(storage)
    }
    if not market_ids:
        return []
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentRow)
                .where(AgentRow.id.in_(market_ids))
                .order_by(
                    AgentRow.updated_at.desc(),
                    AgentRow.id.desc(),  # 同 updated_at 时保证顺序稳定
                ),
            )
        ).scalars().all()
    return list(rows)


async def _session_heat(
    storage: StorageBase,
    market_agent_ids: set[str],
) -> dict[str, int]:
    """实时热度：sessions 表按 ``agent_id`` 聚合的会话数。

    **按市场智能体集合过滤，而不是按会话的 user_id 过滤**：
    sessions.user_id 记的是**使用者**，不是
    智能体拥有者。若按拥有者筛，普通用户（如 lrm）使用市场智能体
    产生的会话就会被漏掉，热度永远只统计运营自己的量。改为
    ``agent_id ∈ 市场智能体集合`` 后，任何人对市场内智能体的使用
    都计入该智能体热度。
    """
    if not market_agent_ids:
        return {}
    factory = getattr(storage, "_session_factory", None)
    if factory is None:
        return {}
    async with factory() as session:
        counts = (
            await session.execute(
                select(SessionRow.agent_id, func.count(SessionRow.id))
                .where(SessionRow.agent_id.in_(market_agent_ids))
                .group_by(SessionRow.agent_id),
            )
        ).all()
    return {agent_id: int(count) for agent_id, count in counts}


async def _tag_map(storage: StorageBase) -> dict[str, str]:
    """{agent_id: tag} 市场名单标签映射（未打标 = 空串，不进映射）。"""
    return {
        e.agent_id: e.tag
        for e in await list_market_entries(storage)
        if e.tag
    }


def _row_to_view(
    row: AgentRow,
    tag: str | None,
    heat: int,
) -> MarketAgentView:
    # payload 存储契约见 _market_agent_rows docstring：名称/提示词嵌在
    # payload["data"] 下（AgentData.name / AgentData.system_prompt）；
    # get("data", {}) 兜底防御历史脏数据。
    data = row.payload.get("data", {}) if isinstance(row.payload, dict) else {}
    return MarketAgentView(
        id=row.id,
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        system_prompt=str(data.get("system_prompt", "")),
        source=row.source,
        tag=tag or "",  # 名单内必有行，None 只是防御；空串 = 未打标
        heat=heat,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _require_market_agent(
    storage: StorageBase,
    agent_id: str,
) -> AgentRow:
    """打标/撕标公共前置校验，失败直接抛 HTTPException。

    - 智能体不存在 → 404；
    - 不在市场名单内（``agent_market`` 无行）→ 404：打标不允许
      隐式上架，上架只能走运营手动 INSERT 或 publish 接口。
    """
    record = await _find_agent_row(storage, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"智能体不存在: {agent_id}",
        )
    if await get_market_entry(storage, agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="该智能体不在市场名单内（agent_market 无记录），不能打标。",
        )
    return record


# ---------------------------------------------------------------------------
# 查询端点
# ---------------------------------------------------------------------------


@market_router.get(
    # 注意：路径必须是空串（prefix 已含 /agent/market）。若写 "/"，
    # 注册路径会变成 "/agent/market/"（尾巴斜杠），请求 /agent/market
    # 时无完全匹配，会被 agent_router 的 PATCH /agent/{agent_id} 部分
    # 匹配吞掉返回 405（/featured 等精确路径不受影响，极隐蔽）。
    "",
    response_model=MarketListResponse,
    summary="平台市场智能体列表",
)
async def list_market_agents(
    tag: str | None = Query(
        default=None,
        description=(
            "按标签筛选。传了该参数时，未打标的智能体不会出现在结果里；"
            "不传则全部返回（tag='' 表示未打标）。"
        ),
    ),
    page_num: int = Query(
        default=1,
        ge=1,
        alias="pageNum",
        description="Page number, 1-based.",
    ),
    page_size: int = Query(
        default=10,
        ge=1,
        le=100,
        alias="pageSize",
        description="Page size (items per page), 1-100.",
    ),
    storage: StorageBase = Depends(get_storage),
) -> MarketListResponse:
    """市场列表：``agent_market`` 名单内全部智能体。

    默认按 ``updated_at`` 倒序（最近更新/上架在前）——热度排序是精选
    推荐接口的专职，本接口不掺和。附带实时热度与标签供前端展示。
    """
    rows = await _market_agent_rows(storage)
    tags = await _tag_map(storage)
    heats = await _session_heat(storage, {r.id for r in rows})

    items: list[MarketAgentView] = []
    for row in rows:
        t = tags.get(row.id, "")
        if tag is not None and t != tag:
            continue
        items.append(_row_to_view(row, t, heats.get(row.id, 0)))

    total = len(items)
    start = (page_num - 1) * page_size
    return MarketListResponse(
        agents=items[start : start + page_size],
        total=total,
    )


@market_router.get(
    "/featured",
    response_model=MarketListResponse,
    summary="精选推荐：实时热度倒序取前 N",
)
async def featured_market_agents(
    top: int = Query(
        default=4,
        ge=1,
        le=50,
        description="取前 N 个，默认 4（可传 3）。",
    ),
    storage: StorageBase = Depends(get_storage),
) -> MarketListResponse:
    """精选推荐：按实时热度（sessions 会话数）倒序取前 N。

    范围同市场列表（``agent_market`` 名单内全部）。
    - 热度是**累计使用量**：刚发布的智能体热度为 0，属正常状态，
      不做 ``>0`` 之类的过滤；
    - 同分的（如上线初期大家都是 0 分）按 ``updated_at`` 倒序兜底，
      保证顺序稳定、页面不空窗；
    - 取不满 N 个就返回实际条数。
    """
    rows = await _market_agent_rows(storage)
    tags = await _tag_map(storage)
    heats = await _session_heat(storage, {r.id for r in rows})

    ranked = sorted(
        rows,
        key=lambda r: (heats.get(r.id, 0), r.updated_at),
        reverse=True,
    )[:top]

    return MarketListResponse(
        agents=[
            _row_to_view(r, tags.get(r.id, ""), heats.get(r.id, 0))
            for r in ranked
        ],
        total=len(ranked),
    )


# ---------------------------------------------------------------------------
# 标签清单端点（全开放）：前端筛选下拉框数据源
# ---------------------------------------------------------------------------


@market_router.get(
    "/tags",
    summary="市场已使用的标签清单（去重，升序）",
)
async def list_market_tag_options(
    storage: StorageBase = Depends(get_storage),
) -> dict:
    """全量标签：``agent_market.tag`` 现存量去重（空串不进清单）。

    自由标签口径下不存在预设清单（旧版 ``config.yaml`` 的
    ``domains`` 已废），这是前端标签下拉框的唯一真实数据源。
    """
    return {"tags": await list_market_tags(storage)}


# ---------------------------------------------------------------------------
# 标签管理端点（全开放，无权限门槛；仅市场名单内智能体）
# ---------------------------------------------------------------------------


@market_router.put(
    "/{agent_id}",
    response_model=MarketEntryView,
    summary="设置智能体标签（全开放，仅市场名单内）",
)
async def upsert_market_agent(
    agent_id: str,
    body: MarketUpsertRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> MarketEntryView:
    """给市场名单内的智能体打标 / 撕标。

    - ``tag`` 是自由字符串（≤64 字符），原样存储，**不做清单校验**；
    - 传空串 = **撕标**（``tag`` 置空，回到"未打标"状态），
      与 ``DELETE /agent/market/{agent_id}`` 等效；
    - 智能体必须在市场名单内（``agent_market`` 有行），否则 404——
      打标不允许隐式上架；上架走运营手动 INSERT 或 publish 接口；
    - 智能体本体不存在 404；
    - 热度不在本接口的管理范围内——热度是实时聚合值，无可配置项。
    """
    tag = (body.tag or "").strip()
    if len(tag) > _TAG_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"标签最长 {_TAG_MAX_LEN} 字符。",
        )
    await _require_market_agent(storage, agent_id)

    await set_market_tag(storage, agent_id, tag)
    saved = await get_market_entry(storage, agent_id)
    assert saved is not None  # 前置校验已确认在名单内
    log_audit(
        user_id,
        "set_agent_tag" if tag else "clear_agent_tag",
        target=agent_id,
        detail=f"tag={tag}" if tag else "撕标（tag 置空）",
    )
    return MarketEntryView(**saved.model_dump())


@market_router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="撕标：清空标签（全开放，仅市场名单内）",
)
async def delete_market_agent(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """撕标：把标签真正撕掉（``tag`` 置空 = 未打标）。

    纯标签操作，**不影响市场名单本身**（把智能体移出市场走
    ``POST /agent/market/{agent_id}/unpublish``）。智能体本体不动；
    幂等：重复调用效果一致。不在市场名单内 404。
    """
    await _require_market_agent(storage, agent_id)
    await set_market_tag(storage, agent_id, "")
    log_audit(
        user_id,
        "clear_agent_tag",
        target=agent_id,
        detail="撕标（tag 置空）",
    )


# ---------------------------------------------------------------------------
# 上架管理（仅智能体 owner）
# ---------------------------------------------------------------------------


def _publish_guard(
    agent_id: str,
    record: AgentRow,
    user_id: str,
) -> None:
    """上架/撤回的公共前置校验，失败直接抛 HTTPException。

    - 非 owner（X-User-ID ≠ agents.user_id）→ 403。
    """
    if record.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="只有智能体拥有者可以发布/撤回发布。",
        )


@market_router.post(
    "/{agent_id}/publish",
    response_model=MarketEntryView,
    summary="发布智能体到市场（仅 owner）",
)
async def publish_market_agent(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> MarketEntryView:
    """发布 = 往 ``agent_market`` 表**插一行**（有行 = 在市场）。

    - 仅智能体 owner（X-User-ID 必须等于 agents.user_id）可调；
    - 不存在 404；
    - 已在名单内则幂等（不覆盖已有标签），返回当前名单记录；
    - 新上架行默认未打标（tag 为空串），上架后可打标。
    """
    record = await _find_agent_row(storage, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"智能体不存在: {agent_id}",
        )
    _publish_guard(agent_id, record, user_id)

    existing = await get_market_entry(storage, agent_id)
    if existing is None:
        await insert_market_entry(storage, agent_id)
        saved = await get_market_entry(storage, agent_id)
        assert saved is not None  # 刚插入，必在
        log_audit(
            user_id,
            "publish_agent_market",
            target=agent_id,
            detail="发布上架（插入市场名单）",
        )
    else:
        saved = existing
        log_audit(
            user_id,
            "publish_agent_market",
            target=agent_id,
            detail="重复发布（已在名单内，幂等）",
        )
    return MarketEntryView(**saved.model_dump())


@market_router.post(
    "/{agent_id}/unpublish",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="撤回发布（仅 owner）",
)
async def unpublish_market_agent(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """撤回 = 从 ``agent_market`` 表**删掉这一行**。

    - 仅智能体 owner 可调（权限口径同 publish）；
    - 不存在 404；
    - 撤回后智能体从市场列表/精选消失；**标签随行删除**（重新发布
      需重打标签）——与旧口径"撤回保留标签"不同；
    - 幂等：不在名单内也返回 204。
    """
    record = await _find_agent_row(storage, agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"智能体不存在: {agent_id}",
        )
    _publish_guard(agent_id, record, user_id)

    await delete_market_entry(storage, agent_id)
    log_audit(
        user_id,
        "unpublish_agent_market",
        target=agent_id,
        detail="撤回发布（删除市场名单行，标签随行消失）",
    )


__all__ = ["market_router"]
