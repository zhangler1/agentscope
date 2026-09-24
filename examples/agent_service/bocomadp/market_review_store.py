# -*- coding: utf-8 -*-
"""智能体市场发布审批表（``agent_market_review``）+ 异步访问层。

与 ``market_store.py`` 同一套模式：独立 declarative base，复用框架
存储的 ``_engine`` / ``_session_factory``，``ensure_review_tables``
幂等建表，``src/`` 框架零改动。

表语义（**一智能体一记录**，按 ``agent_id`` upsert 覆盖，不留历史流水）：

- ``agent_id``    主键 = ``agents.id``（1:1，删智能体级联删行）；
- ``applicant``   申请人（owner user_id）；
- ``status``      pending / approved / rejected；
- ``reason``      拒绝理由（其他状态空串）；
- ``reviewer`` / ``reviewed_at``  最近一次审批人/时间（重新 publish 清空）；
- ``created_at`` / ``updated_at`` 申请/最近变更时间。

状态机::

    未提交 ─ publish → pending ─ approve → approved（同时插 agent_market 名单行）
                        └─ reject ─→ rejected ─ 重新 publish（清旧结论）→ pending

单记录覆盖方案下 ``reviewer`` 的语义是"**最近一次审批人**"——同一
智能体被拒过又批过（重新发布），表里只剩最终一轮的结论；上一轮的
痕迹只存在于 ``[AGENT_MARKET_AUDIT]`` 控制台日志。审批表里**不存**
智能体名称/提示词快照：列表展示时从 ``agents`` 表现查（审的永远是
当前最新内容）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import DateTime, Index, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("bocomadp.market_review_store")

#: 待审批（发布申请已提交，等审批人处理）
STATUS_PENDING = "pending"
#: 已通过（同时已在 agent_market 名单内）
STATUS_APPROVED = "approved"
#: 已拒绝（reason 带理由，可修改后重新 publish）
STATUS_REJECTED = "rejected"

_VALID_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

#: "从未提交过发布"的合成状态（表里无记录时返回，
#: 不落库）
STATUS_NOT_SUBMITTED = "not_submitted"

#: 拒绝理由长度限制（与接口 422 校验一致）
REASON_MAX_LEN = 200


class _ReviewBase(DeclarativeBase):
    """bocomadp 专用 declarative base：只服务 agent_market_review 表。"""


class AgentMarketReviewRow(_ReviewBase):
    __tablename__ = "agent_market_review"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    applicant: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_PENDING,
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reviewer: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 发布档案：publish 弹窗填写的部门/系统/业务条线/说明，
    # 审批人据此判断批不批；approve 上架时复制进 agent_market 行。
    # 业务条线即 tag（与 agent_market.tag / 打标接口同一字段）；
    # 列名统一 system_name（存的是"系统名"，比 system 更达意），
    # 与接口字段同名免转换。
    department: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    system_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tag: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)

    __table_args__ = (Index("ix_agent_market_review_status", "status"),)


class AgentMarketReview(BaseModel):
    """一条发布审批记录（对应表里的一行）。"""

    agent_id: str
    applicant: str = ""
    status: str = STATUS_PENDING
    reason: str = ""
    reviewer: str = ""
    department: str = ""
    system_name: str = ""
    tag: str = ""
    description: str = ""
    reviewed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _now() -> datetime:
    """DateTime() 无时区列统一用 UTC naive 时间（与 market_store 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_factory(storage: Any) -> Any:
    return getattr(storage, "_session_factory", None)


def _row_to_entry(row: AgentMarketReviewRow) -> AgentMarketReview:
    return AgentMarketReview(
        agent_id=row.agent_id,
        applicant=row.applicant,
        status=row.status,
        reason=row.reason,
        reviewer=row.reviewer,
        department=row.department,
        system_name=row.system_name,
        tag=row.tag,
        description=row.description,
        reviewed_at=row.reviewed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def ensure_review_tables(storage: Any) -> None:
    """启动时建表（幂等）+ 列级迁移 + 孤儿审批行清理。"""
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning(
            "storage has no _engine yet; skip agent_market_review "
            "table provisioning",
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_ReviewBase.metadata.create_all)
    # 老库补列（create_all 只按表名跳过，不会给已存在的表加新列）
    from bocomadp.market_store import ensure_columns

    await ensure_columns(
        engine,
        {
            "agent_market_review": {
                "department": "VARCHAR(64) NOT NULL DEFAULT ''",
                "system_name": "VARCHAR(64) NOT NULL DEFAULT ''",
                "tag": "VARCHAR(64) NOT NULL DEFAULT ''",
                "description": "VARCHAR(500) NOT NULL DEFAULT ''",
            },
        },
    )
    logger.info("ensured table agent_market_review")

    pruned = await prune_orphan_reviews(storage)
    if pruned:
        logger.info("pruned %d orphan agent_market_review rows", pruned)


async def get_review_record(
    storage: Any,
    agent_id: str,
) -> AgentMarketReview | None:
    """按 agent_id 取审批记录；无记录（未提交过）返回 None。"""
    factory = _session_factory(storage)
    if factory is None:
        return None
    async with factory() as session:
        row = await session.get(AgentMarketReviewRow, agent_id)
        return None if row is None else _row_to_entry(row)


async def upsert_pending_review(
    storage: Any,
    agent_id: str,
    applicant: str,
    meta: dict[str, str] | None = None,
) -> AgentMarketReview:
    """提交/重新提交发布申请：记录置为 pending（upsert）。

    - 无记录 → 插入（created_at = now）；
    - 已 pending → 原样返回（幂等，不刷新 created_at——先到先审的
      排序依据不能被重复点击搅乱）；但**发布档案（meta）照常覆盖**
      ——用户改完弹窗表单再点发布，存的信息必须是最新的；
    - rejected / approved → 重置回 pending 并**清空旧结论**
      （reason / reviewer / reviewed_at 作废，updated_at = now）。

    ``meta`` = 发布档案 ``{"department", "system_name", "tag",
    "description"}``（tag 即业务条线），缺键兜底空串。
    """
    factory = _session_factory(storage)
    if factory is None:  # pragma: no cover - 无存储时业务层早就 503 了
        raise RuntimeError("storage has no session factory")
    now = _now()
    meta = meta or {}
    async with factory() as session:
        row = await session.get(AgentMarketReviewRow, agent_id)
        if row is None:
            row = AgentMarketReviewRow(
                agent_id=agent_id,
                applicant=applicant,
                status=STATUS_PENDING,
                reason="",
                reviewer="",
                department=meta.get("department", ""),
                system_name=meta.get("system_name", ""),
                tag=meta.get("tag", ""),
                description=meta.get("description", ""),
                reviewed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        elif row.status != STATUS_PENDING:
            row.status = STATUS_PENDING
            row.applicant = applicant
            row.reason = ""
            row.reviewer = ""
            row.department = meta.get("department", "")
            row.system_name = meta.get("system_name", "")
            row.tag = meta.get("tag", "")
            row.description = meta.get("description", "")
            row.reviewed_at = None
            row.updated_at = now
        else:
            # 已 pending：结论字段不动，仅覆盖表单档案
            row.department = meta.get("department", "")
            row.system_name = meta.get("system_name", "")
            row.tag = meta.get("tag", "")
            row.description = meta.get("description", "")
            row.updated_at = now
        await session.commit()
        return _row_to_entry(row)


async def decide_review(
    storage: Any,
    agent_id: str,
    *,
    status: str,
    reviewer: str,
    reason: str = "",
) -> AgentMarketReview | None:
    """审批落锤：pending → approved / rejected（写入审批人/理由/时间）。

    仅允许从 pending 出发——对已办结（approved/rejected）的记录重复
    审批返回 None（路由层映射 409）。``status`` 必须是终态之一。
    """
    if status not in (STATUS_APPROVED, STATUS_REJECTED):
        raise ValueError(f"decision status must be approved/rejected: {status}")
    factory = _session_factory(storage)
    if factory is None:
        return None
    async with factory() as session:
        row = await session.get(AgentMarketReviewRow, agent_id)
        if row is None or row.status != STATUS_PENDING:
            return None
        row.status = status
        row.reviewer = reviewer
        row.reason = reason
        row.reviewed_at = _now()
        row.updated_at = row.reviewed_at
        await session.commit()
        return _row_to_entry(row)


async def delete_review_record(storage: Any, agent_id: str) -> bool:
    """删审批记录（unpublish 撤申请 / 删智能体级联共用）。删到 True。"""
    factory = _session_factory(storage)
    if factory is None:
        return False
    async with factory() as session:
        row = await session.get(AgentMarketReviewRow, agent_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def list_reviews_by_status(
    storage: Any,
    status: str,
) -> list[AgentMarketReview]:
    """按状态取全部审批记录（审批列表数据源；排序/分页在路由层做）。"""
    if status not in _VALID_STATUSES:
        return []
    factory = _session_factory(storage)
    if factory is None:
        return []
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentMarketReviewRow).where(
                    AgentMarketReviewRow.status == status,
                ),
            )
        ).scalars().all()
        return [_row_to_entry(r) for r in rows]


async def list_reviews(
    storage: Any,
    status: str | None = None,
) -> list[AgentMarketReview]:
    """按状态取审批记录；``status=None``（或 "all"）= 全部三种状态。

    审核工作台条件查询的数据源（排序/模糊过滤/分页在路由层做）。
    与 :func:`list_reviews_by_status` 的差别仅在支持"不过滤状态"——
    保留旧函数不动（已有调用方语义不变），本函数是全量版。
    """
    if status == "all":
        status = None
    if status is not None and status not in _VALID_STATUSES:
        return []
    factory = _session_factory(storage)
    if factory is None:
        return []
    async with factory() as session:
        stmt = select(AgentMarketReviewRow)
        if status is not None:
            stmt = stmt.where(AgentMarketReviewRow.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_entry(r) for r in rows]


async def count_reviews_by_status(storage: Any) -> dict[str, int]:
    """按状态 GROUP BY 计数（审核页顶部四张统计卡的数据源）。

    返回 ``{"pending": n, "approved": n, "rejected": n}``（全部 =
    三者相加，路由层算，不查两次）。无记录的状态计 0。
    """
    factory = _session_factory(storage)
    counts = {s: 0 for s in _VALID_STATUSES}
    if factory is None:
        return counts
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    AgentMarketReviewRow.status,
                    func.count(AgentMarketReviewRow.agent_id),
                ).group_by(AgentMarketReviewRow.status),
            )
        ).all()
    for row_status, count in rows:
        if row_status in counts:
            counts[row_status] = int(count)
    return counts


async def review_status_map(
    storage: Any,
    agent_ids: list[str],
) -> dict[str, AgentMarketReview]:
    """批量取审批记录（``/agent/owned`` 附带发布状态用）。

    两步小查询风格：一次 ``id IN`` 拿全部记录，避免逐条查询。
    返回 ``{agent_id: AgentMarketReview}``，不在内的 id 缺席。
    """
    if not agent_ids:
        return {}
    factory = _session_factory(storage)
    if factory is None:
        return {}
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentMarketReviewRow).where(
                    AgentMarketReviewRow.agent_id.in_(set(agent_ids)),
                ),
            )
        ).scalars().all()
        return {r.agent_id: _row_to_entry(r) for r in rows}


async def prune_orphan_reviews(storage: Any) -> int:
    """清理孤儿审批行（agents 表里已不存在的智能体）。

    删除智能体时路由层会级联删审批行，但团队成员级联删除等绕过
    路径可能漏掉，启动时兜底扫一遍（与 ``market_store`` 同款）。
    """
    factory = _session_factory(storage)
    if factory is None:
        return 0
    try:
        from agentscope.app.storage._sql._tables import AgentRow
    except Exception:  # noqa: BLE001 - 非 SQL 存储（Redis）没有该表
        return 0
    async with factory() as session:
        alive = {
            agent_id
            for (agent_id,) in (
                await session.execute(select(AgentRow.id))
            ).all()
        }
        rows = (
            await session.execute(select(AgentMarketReviewRow))
        ).scalars().all()
        orphans = [r for r in rows if r.agent_id not in alive]
        for row in orphans:
            await session.delete(row)
        if orphans:
            await session.commit()
        return len(orphans)


__all__ = [
    "AgentMarketReview",
    "AgentMarketReviewRow",
    "REASON_MAX_LEN",
    "STATUS_APPROVED",
    "STATUS_NOT_SUBMITTED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "decide_review",
    "delete_review_record",
    "ensure_review_tables",
    "get_review_record",
    "list_reviews",
    "list_reviews_by_status",
    "prune_orphan_reviews",
    "review_status_map",
    "count_reviews_by_status",
    "upsert_pending_review",
]
