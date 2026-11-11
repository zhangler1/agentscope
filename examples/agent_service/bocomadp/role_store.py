# -*- coding: utf-8 -*-
"""角色表（``roles``）+ 用户角色关联表（``user_roles``）+ 异步访问层。

两表方案（第一版只落 ``reviewer`` 一个角色，能力写死在 ``has_role`` 判断
里，不做权限维度拆分；将来若要同角色内能力拆分再升级三表）：

- ``roles``：有哪些角色（``role_key`` 主键 + 展示名 + 描述）；
- ``user_roles``：哪些用户拥有哪些角色（``user_id`` + ``role_key``，
  联合唯一，幂等）。

与 ``team_store`` / ``market_store`` 同一套模式：bocomadp 自建 declarative
base，但**复用框架 ``AsyncSQLAlchemyStorage`` 的连接**（通过
``bind_storage(storage)`` 绑定其 ``_engine``），不自己开连接池。启动时
``ensure_tables`` 幂等建表 + 种子首批 reviewer，与 agents / team / market
等表共用同一数据库与连接池（测试里即同一个 SQLite 文件），``src/`` 框架零改动。

首版仅 ``reviewer`` 一个角色；接口层（``market_reviewers``）把"审批人
白名单"语义映射到 ``role_key='reviewer'`` 的 ``user_roles`` 行。末位保护
（至少留 1 人）在接口层实现（删最后一人 / 清空名单返回冲突），种子仅管
冷启动第一人。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    delete,
    insert,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger("bocomadp.role_store")

#: 角色表唯一主键（reviewer = 审批管理员）。

#: 首批 reviewer 种子（仅当 user_roles 表为空时一次性写入，之后由接口管理）。
SEED_REVIEWERS: tuple[str, ...] = (
    "sunw_94",
    "jiangchengwei",
    "jinyul",
    "zhanglm_1",
    "junlinchen",
    "linruimin",
    "guo_zhihao",
)

#: 首版唯一角色。
REVIEWER_ROLE: str = "reviewer"


class _RoleBase(DeclarativeBase):
    """bocomadp 专用 declarative base：只服务角色两张表。"""


class RoleRow(_RoleBase):
    __tablename__ = "roles"

    role_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    role_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class UserRoleRow(_RoleBase):
    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(
        # 跨方言自增主键：MySQL 落 BIGINT AUTO_INCREMENT（2^63 范围），
        # SQLite 经 with_variant 落 INTEGER PRIMARY KEY（否则 BIGINT 在该方言
        # 下不被识别为 rowid 别名、不自增）。对齐 memory/_sql_memory.py 写法。
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role_key: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("user_id", "role_key", name="uq_user_role"),
    )


_engine: Any = None


def bind_storage(storage: Any) -> None:
    """绑定框架 ``AsyncSQLAlchemyStorage`` 的 engine，复用其连接。

    角色两表不自己开连接，而是复用框架已建立的 ``storage._engine``，
    与 agents / team / market 等表共用同一数据库与连接池（测试里即同一个
    SQLite 文件）。任何读写前必须先绑定：``main.py`` 启动与测试 fixture
    负责调用。未绑定时读写函数会安全返回空结果（不报错）。
    """
    global _engine
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning("storage has no _engine; role_store cannot bind")
        return
    _engine = engine


async def _get_engine() -> Any:
    """返回已绑定的框架 storage engine；未绑定时返回 ``None``。"""
    if _engine is None:
        logger.warning(
            "role_store not bound to a storage engine; call bind_storage(storage) first",
        )
    return _engine


async def ensure_tables() -> None:
    """幂等建表 + 种子首批 reviewer（user_roles 为空时）。"""
    engine = await _get_engine()
    if engine is None:
        return
    # MySQL 用 INSERT IGNORE；SQLite 用 INSERT OR IGNORE（测试环境）。
    ignore_kw = "OR IGNORE" if engine.dialect.name == "sqlite" else "IGNORE"
    async with engine.begin() as conn:
        await conn.run_sync(_RoleBase.metadata.create_all)
    # 确保 reviewer 角色存在（INSERT IGNORE 语义，重复无害）。
    async with engine.begin() as conn:
        await conn.execute(
            insert(RoleRow).values(
                role_key=REVIEWER_ROLE,
                role_name="审批管理员",
                description="可查看全部智能体列表并审批发布申请",
                created_at=datetime.now(),
            ).prefix_with(ignore_kw),
        )
    # 冷启动：user_roles 为空才种子，之后完全由接口管理。
    members = await list_role_members(REVIEWER_ROLE)
    if not members:
        async with engine.begin() as conn:
            for uid in SEED_REVIEWERS:
                await conn.execute(
                    insert(UserRoleRow)
                    .values(user_id=uid, role_key=REVIEWER_ROLE)
                    .prefix_with(ignore_kw),
                )
        logger.info(
            "seeded %d initial reviewers into user_roles", len(SEED_REVIEWERS),
        )


async def has_role(user_id: str, role_key: str) -> bool:
    """判断 user_id 是否拥有 role_key 角色。"""
    if not user_id:
        return False
    engine = await _get_engine()
    if engine is None:
        return False
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(UserRoleRow.user_id).where(
                    UserRoleRow.user_id == user_id,
                    UserRoleRow.role_key == role_key,
                ),
            )
        ).first()
    return row is not None


async def list_role_members(role_key: str) -> list[str]:
    """某角色的全部成员 user_id。"""
    engine = await _get_engine()
    if engine is None:
        return []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(UserRoleRow.user_id).where(
                    UserRoleRow.role_key == role_key,
                ),
            )
        ).scalars().all()
    return list(rows)


async def list_my_roles(user_id: str) -> list[str]:
    """某用户拥有的全部角色 key（前端据此判断权限入口）。"""
    if not user_id:
        return []
    engine = await _get_engine()
    if engine is None:
        return []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(UserRoleRow.role_key).where(
                    UserRoleRow.user_id == user_id,
                ),
            )
        ).scalars().all()
    return list(rows)


async def assign_role(user_id: str, role_key: str) -> int:
    """新增单条（幂等），返回实际新增条数（0=已存在 / 空白）。"""
    uid = (user_id or "").strip()
    if not uid:
        return 0
    if await has_role(uid, role_key):
        return 0
    engine = await _get_engine()
    if engine is None:
        return 0
    async with engine.begin() as conn:
        await conn.execute(
            insert(UserRoleRow).values(user_id=uid, role_key=role_key),
        )
    return 1


async def revoke_role(user_id: str, role_key: str) -> str:
    """删单条：``not_found`` / ``last_one``（末位保护）/ ``ok``。"""
    uid = (user_id or "").strip()
    members = await list_role_members(role_key)
    if uid not in members:
        return "not_found"
    if len(members) == 1:
        return "last_one"
    engine = await _get_engine()
    if engine is None:
        return "not_found"
    async with engine.begin() as conn:
        await conn.execute(
            delete(UserRoleRow).where(
                UserRoleRow.user_id == uid,
                UserRoleRow.role_key == role_key,
            ),
        )
    return "ok"


async def overwrite_role_members(role_key: str, user_ids: list[str]) -> str:
    """全量覆盖某角色成员：空清单返回 ``empty``（末位保护），否则 ``ok``。"""
    fresh = {(u or "").strip() for u in (user_ids or [])}
    fresh.discard("")
    if not fresh:
        return "empty"
    engine = await _get_engine()
    if engine is None:
        return "empty"
    async with engine.begin() as conn:
        await conn.execute(
            delete(UserRoleRow).where(UserRoleRow.role_key == role_key),
        )
        for uid in fresh:
            await conn.execute(
                insert(UserRoleRow).values(user_id=uid, role_key=role_key),
            )
    return "ok"


__all__ = [
    "REVIEWER_ROLE",
    "SEED_REVIEWERS",
    "assign_role",
    "bind_storage",
    "ensure_tables",
    "has_role",
    "list_my_roles",
    "list_role_members",
    "overwrite_role_members",
    "revoke_role",
]
