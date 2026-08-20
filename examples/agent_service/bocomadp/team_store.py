# -*- coding: utf-8 -*-
"""Expert-team relation table + async access layer.

专家团"团队关系"独立表。原先 ``TeamConfig`` / ``parent_agent_id`` 内嵌在
框架 ``src/`` 的 ``AgentData`` 里（名片背面贴便利贴），本次迁移把它们全部
摘出，放进这张独立档案表——框架 ``src/`` 恢复成对团队零感知的干净状态，
所有专家团关系数据与读写逻辑都归 ``bocomadp`` 管理。

表语义（一行 = 一个 leader 的团队档案）:

- ``user_id`` + ``leader_agent_id`` 联合主键（谁的档案室、哪张团长名片）
- ``members``: 成员名册，每条带 ``relation`` 标记
  - ``self_built`` 自建（团长创建的子 agent，leader 删除时级联删）
  - ``invited``   外邀（引用别人的 agent，只摘链接不删人）
- ``handoff_relations``: 交接序（workflow 模式的严格交接链）
- ``collaboration_mode``: ``free_handoff``（自由交接）| ``workflow``（固定流程）

访问层复用框架存储引擎（``AsyncSQLAlchemyStorage._engine`` /
``_session_factory``），在启动时 ``ensure_team_tables`` 建表。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("bocomadp.team_store")

# ---------------------------------------------------------------------------
# 业务模型（原 src/…/storage/_model/_agent.py 迁出，外加 relation 标记）
# ---------------------------------------------------------------------------


class HandoffRelation(BaseModel):
    """一条"可交接给谁"的有向边：from 可以把任务交给 to。"""

    from_agent_id: str
    to_agent_id: str
    description: str = ""


MemberRelation = Literal["self_built", "invited"]


class ExpertTeamMember(BaseModel):
    """名册条目；``relation`` 说明这个成员是怎么进来的。"""

    agent_id: str
    relation: MemberRelation = "self_built"


class ExpertTeamRelation(BaseModel):
    """一张团队档案（对应表里的一行）。"""

    user_id: str
    leader_agent_id: str
    collaboration_mode: str = "free_handoff"
    members: list[ExpertTeamMember] = Field(default_factory=list)
    handoff_relations: list[HandoffRelation] = Field(default_factory=list)
    max_members: int = 10
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # ---- 便捷查询 ----

    @property
    def member_ids(self) -> list[str]:
        """所有成员 id（自建 + 外邀）。"""
        return [m.agent_id for m in self.members]

    def relation_of(self, agent_id: str) -> MemberRelation | None:
        """某个成员的自建/外邀标记；不是成员返回 None。"""
        for m in self.members:
            if m.agent_id == agent_id:
                return m.relation
        return None

    def is_self_built(self, agent_id: str) -> bool:
        """该成员是不是团长自建的（自建才随团长级联删除）。"""
        return self.relation_of(agent_id) == "self_built"

    def add_member(self, agent_id: str, relation: MemberRelation) -> None:
        """幂等加入名册（同 id 更新标记，不重复）。"""
        for m in self.members:
            if m.agent_id == agent_id:
                m.relation = relation
                return
        self.members.append(ExpertTeamMember(agent_id=agent_id, relation=relation))

    def remove_member(self, agent_id: str) -> bool:
        """摘除成员；摘到返回 True，不在名册返回 False。"""
        for i, m in enumerate(self.members):
            if m.agent_id == agent_id:
                del self.members[i]
                return True
        return False


# ---------------------------------------------------------------------------
# SQLAlchemy 表（独立 metadata，启动时与框架表一起建）
# ---------------------------------------------------------------------------


class _TeamBase(DeclarativeBase):
    """bocomadp 专用 declarative base：只服务专家团关系表。"""


class ExpertTeamRelationRow(_TeamBase):
    __tablename__ = "expert_team_relations"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    leader_agent_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    collaboration_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="free_handoff",
    )
    members: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    handoff_relations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    max_members: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


# ---------------------------------------------------------------------------
# 异步访问层（storage 参数就是框架的 storage，含 _engine/_session_factory）
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """PG 的 DateTime() 无时区列用 UTC 时间，去掉 tzinfo 便于存读。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row_to_model(row: Any) -> ExpertTeamRelation:
    return ExpertTeamRelation(
        user_id=row.user_id,
        leader_agent_id=row.leader_agent_id,
        collaboration_mode=row.collaboration_mode,
        members=[ExpertTeamMember(**m) for m in (row.members or [])],
        handoff_relations=[HandoffRelation(**h) for h in (row.handoff_relations or [])],
        max_members=row.max_members,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _model_to_row(rel: ExpertTeamRelation) -> dict[str, Any]:
    now = _now()
    return {
        "user_id": rel.user_id,
        "leader_agent_id": rel.leader_agent_id,
        "collaboration_mode": rel.collaboration_mode,
        "members": [m.model_dump() for m in rel.members],
        "handoff_relations": [h.model_dump() for h in rel.handoff_relations],
        "max_members": rel.max_members,
        "created_at": rel.created_at or now,
        "updated_at": now,
    }


async def ensure_team_tables(storage: Any) -> None:
    """启动时建表（幂等，已存在则跳过）。"""
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning(
            "storage has no _engine yet; skip expert-team table provisioning",
        )
        return
    async with engine.begin() as conn:
        await conn.run_sync(_TeamBase.metadata.create_all)
    logger.info("ensured table expert_team_relations")


async def get_team(
    storage: Any,
    user_id: str,
    leader_agent_id: str,
) -> ExpertTeamRelation | None:
    """按联合主键取一张团队档案。"""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return None
    async with session_factory() as session:
        row = await session.get(
            ExpertTeamRelationRow,
            (user_id, leader_agent_id),
        )
        return _row_to_model(row) if row is not None else None


async def list_teams(storage: Any, user_id: str) -> list[ExpertTeamRelation]:
    """某用户（档案室主人）名下的全部团队档案。"""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return []
    async with session_factory() as session:
        from sqlalchemy import select

        rows = (
            await session.execute(
                select(ExpertTeamRelationRow).where(
                    ExpertTeamRelationRow.user_id == user_id,
                ),
            )
        ).scalars().all()
        return [_row_to_model(r) for r in rows]


async def upsert_team(storage: Any, rel: ExpertTeamRelation) -> None:
    """整行覆盖写（存在则更新，不存在则插入）。"""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return
    data = _model_to_row(rel)
    async with session_factory() as session:
        row = await session.get(
            ExpertTeamRelationRow,
            (rel.user_id, rel.leader_agent_id),
        )
        if row is None:
            session.add(ExpertTeamRelationRow(**data))
        else:
            for key, value in data.items():
                setattr(row, key, value)
        await session.commit()


async def delete_team(storage: Any, user_id: str, leader_agent_id: str) -> None:
    """删除一张团队档案（解散团队）。"""
    session_factory = getattr(storage, "_session_factory", None)
    if session_factory is None:
        return
    async with session_factory() as session:
        row = await session.get(
            ExpertTeamRelationRow,
            (user_id, leader_agent_id),
        )
        if row is not None:
            await session.delete(row)
            await session.commit()


__all__ = [
    "ExpertTeamMember",
    "ExpertTeamRelation",
    "HandoffRelation",
    "MemberRelation",
    "delete_team",
    "ensure_team_tables",
    "get_team",
    "list_teams",
    "upsert_team",
]
