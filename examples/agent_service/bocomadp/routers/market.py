# -*- coding: utf-8 -*-
"""BocomADP agent-market router — 平台市场查询 + 精选推荐 + 标签管理。

领域分类即 ``agent_market.tag`` 自由字符串（≤64 字符）：打标内容
原样存储，无预设清单、无权限门槛（带 X-User-ID 即可调）。平台智能体
创建时自动写入默认标签（config ``agent_market.default_tag``，当前
"未分类"），清标/下架均重置回默认值。

端点（统一挂 ``/agent/market`` 前缀，main.py 再统一加 ``/api``）：

查询类（无权限要求）：
- ``GET  /agent/market``              平台市场列表（user_id=default 全部智能体，
                                       无 source 筛选；按 updated_at 倒序分页，
                                       可按 tag 筛选）；
- ``GET  /agent/market/featured``     精选推荐：按实时热度（sessions 会话数）
                                       倒序取前 N，同分按 updated_at 倒序兜底。

标签管理（无权限要求）：
- ``PUT  /agent/market/{agent_id}``   设置标签（空串 = 重置回默认"未分类"）；
- ``DELETE /agent/market/{agent_id}`` 重置回默认标签（204，等效清标签）。

设计要点：

- **范围**：平台市场只查 ``user_id='default'``（配置
  ``agent_market.platform_user_id``），**不加** ``source`` 筛选；
  普通用户的智能体（user_id=真实用户）只出现在 ``GET /agent/owned``。
- **热度（实时聚合）**：heat 不落库，每次查询时对 ``sessions``
  表按 ``agent_id`` GROUP BY 计数。将来量大再升级为固化分值 +
  定时增量累加，接口签名不变。
- **操作留痕只打控制台日志**：标签操作调用
  :func:`bocomadp.market_audit.log_audit`，以 ``[AGENT_MARKET_AUDIT]``
  前缀打印 INFO，用 ``docker compose logs -f agentscope-service``
  直接查看，不再落库（将来要长期留痕只需替换该模块的日志实现）。
- 框架的 ``AgentRow`` / ``SessionRow`` 是私有模块，但 bocomadp 层
  访问框架私有成员（``_engine`` / ``_session_factory``）已有
  ``team_store.py`` 先例，此处一致：只读不写框架表。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

logger = logging.getLogger("bocomadp.market_router")

from agentscope.app.deps import get_current_user_id, get_storage
from agentscope.app.storage import StorageBase
from agentscope.app.storage._sql._tables import AgentRow, SessionRow

from bocomadp.config.market_config import get_default_tag, get_platform_user_id
from bocomadp.market_audit import log_audit
from bocomadp.market_store import (
    AgentMarketEntry,
    get_market_entry,
    list_market_entries,
    upsert_market_entry,
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


# 系统内置智能体的 id 约定：以 "_" 开头（如 _agent-creator 智能体工厂）。
# 它们是内部工具载体，不作为市场商品对用户露出，也不可被打标。
_SYSTEM_AGENT_PREFIX = "_"


def _is_system_agent(agent_id: str) -> bool:
    return agent_id.startswith(_SYSTEM_AGENT_PREFIX)


async def _platform_agent_rows(storage: StorageBase) -> list[AgentRow]:
    """平台名下（user_id=default）的全部 agents 行，updated_at 倒序。

    注意：**不做 source 筛选**——按需求约定，平台市场的范围只由
    user_id 决定；但**排除系统内置智能体**（``_`` 开头的 id，如
    ``_agent-creator`` 智能体工厂）——它们是内部工具载体，不应作为
    市场商品露出。返回完整 ORM 行（名称在 ``payload["data"]["name"]``，
    框架 mapper 的存储契约：payload = record.model_dump 去掉
    id/created_at/updated_at/user_id/source 后的剩余部分，即
    ``{"data": {...AgentData...}}``）。
    """
    factory = getattr(storage, "_session_factory", None)
    if factory is None:
        return []
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentRow)
                .where(AgentRow.user_id == get_platform_user_id())
                .order_by(
                    AgentRow.updated_at.desc(),
                    AgentRow.id.desc(),  # 同 updated_at 时保证顺序稳定
                ),
            )
        ).scalars().all()
    return [r for r in rows if not _is_system_agent(r.id)]


async def _session_heat(
    storage: StorageBase,
    platform_agent_ids: set[str],
) -> dict[str, int]:
    """实时热度：sessions 表按 ``agent_id`` 聚合的会话数。

    **按平台智能体集合过滤，而不是按会话的 user_id 过滤**：
    sessions.user_id 记的是**使用者**，不是
    智能体拥有者。若按 ``user_id = 'default'`` 筛，普通用户（如 lrm）
    使用平台智能体产生的会话就会被漏掉，热度永远只统计运营自己的量。
    改为 ``agent_id ∈ 平台智能体集合`` 后，任何人对平台智能体的使用
    都计入该智能体热度；用户使用自己的私有智能体则天然不在集合内。
    """
    if not platform_agent_ids:
        return {}
    factory = getattr(storage, "_session_factory", None)
    if factory is None:
        return {}
    async with factory() as session:
        counts = (
            await session.execute(
                select(SessionRow.agent_id, func.count(SessionRow.id))
                .where(SessionRow.agent_id.in_(platform_agent_ids))
                .group_by(SessionRow.agent_id),
            )
        ).all()
    return {agent_id: int(count) for agent_id, count in counts}


async def _tag_map(storage: StorageBase) -> dict[str, str]:
    """{agent_id: tag} 市场档案映射（未建档/空串视为未打标）。"""
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
    # payload 存储契约见 _platform_agent_rows docstring：名称嵌在
    # payload["data"]["name"]；get("data", {}) 兜底防御历史脏数据。
    data = row.payload.get("data", {}) if isinstance(row.payload, dict) else {}
    return MarketAgentView(
        id=row.id,
        name=str(data.get("name", "")),
        source=row.source,
        tag=tag,
        heat=heat,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


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
            "不传则全部返回（tag=null 表示未打标）。"
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
    """平台市场列表：user_id=default 的全部智能体（无 source 筛选）。

    默认按 ``updated_at`` 倒序（最近更新/上架在前）——热度排序是精选
    推荐接口的专职，本接口不掺和。附带实时热度与标签供前端展示。
    """
    rows = await _platform_agent_rows(storage)
    tags = await _tag_map(storage)
    heats = await _session_heat(storage, {r.id for r in rows})

    items: list[MarketAgentView] = []
    for row in rows:
        t = tags.get(row.id)
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

    - 热度是**累计使用量**：刚发布的智能体热度为 0，属正常状态，
      不做 ``>0`` 之类的过滤；
    - 同分的（如上线初期大家都是 0 分）按 ``updated_at`` 倒序兜底，
      保证顺序稳定、页面不空窗；
    - 取不满 N 个就返回实际条数。
    """
    rows = await _platform_agent_rows(storage)
    tags = await _tag_map(storage)
    heats = await _session_heat(storage, {r.id for r in rows})

    ranked = sorted(
        rows,
        key=lambda r: (heats.get(r.id, 0), r.updated_at),
        reverse=True,
    )[:top]

    return MarketListResponse(
        agents=[
            _row_to_view(r, tags.get(r.id), heats.get(r.id, 0))
            for r in ranked
        ],
        total=len(ranked),
    )


# ---------------------------------------------------------------------------
# 标签管理端点（全开放，无权限门槛）
# ---------------------------------------------------------------------------


@market_router.put(
    "/{agent_id}",
    response_model=MarketEntryView,
    summary="设置智能体标签（全开放）",
)
async def upsert_market_agent(
    agent_id: str,
    body: MarketUpsertRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> MarketEntryView:
    """为平台智能体建档/更新标签。

    - ``tag`` 是自由字符串（≤64 字符），原样存储，**不做清单
      校验**；传空串表示**重置回默认标签**（config ``default_tag``，
      当前"未分类"）——不删档案行；
    - 目标智能体必须真实存在且挂在平台 user 名下（404）；
    - 热度不在本接口的管理范围内——热度是实时聚合值，无可配置项。
    """
    tag = (body.tag or "").strip()
    if len(tag) > _TAG_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"标签最长 {_TAG_MAX_LEN} 字符。",
        )
    if _is_system_agent(agent_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="系统内置智能体不参与市场打标。",
        )

    record = await storage.get_agent(get_platform_user_id(), agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"平台智能体不存在: {agent_id}",
        )

    if not tag:
        # 空串 = 重置回默认标签（"未分类"），不删档案行
        tag = get_default_tag()
        action, detail = "reset_agent_tag", f"重置为默认标签 {tag}"
    else:
        action, detail = "set_agent_tag", f"tag={tag}"

    existing = await get_market_entry(storage, agent_id)
    await upsert_market_entry(
        storage,
        AgentMarketEntry(
            agent_id=agent_id,
            tag=tag,
            created_at=existing.created_at if existing else None,
        ),
    )
    saved = await get_market_entry(storage, agent_id)
    assert saved is not None  # 刚写入，必在
    log_audit(user_id, action, target=agent_id, detail=detail)
    return MarketEntryView(**saved.model_dump())


@market_router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="下架：重置回默认标签（全开放）",
)
async def delete_market_agent(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """下架：清除自定义标签，重置回默认值（"未分类"）。

    智能体本体不动，档案行也保留（"未分类"也是一种标签）；
    幂等：重复调用效果一致。系统内置智能体 422 拒绝。
    """
    if _is_system_agent(agent_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="系统内置智能体不参与市场打标。",
        )
    record = await storage.get_agent(get_platform_user_id(), agent_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"平台智能体不存在: {agent_id}",
        )
    default_tag = get_default_tag()
    existing = await get_market_entry(storage, agent_id)
    await upsert_market_entry(
        storage,
        AgentMarketEntry(
            agent_id=agent_id,
            tag=default_tag,
            created_at=existing.created_at if existing else None,
        ),
    )
    log_audit(
        user_id,
        "reset_agent_tag",
        target=agent_id,
        detail=f"下架重置为默认标签 {default_tag}",
    )


__all__ = ["market_router"]
