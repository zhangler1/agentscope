# -*- coding: utf-8 -*-
"""智能体市场审批人白名单（审批人角色 ``reviewer`` 的名单视图）。

数据真源已迁移到 :mod:`bocomadp.role_store` 的两表（``roles`` /
``user_roles``）；本模块只保留接口层所需的"审批人"语义封装——
``reviewer`` 角色的成员即审批人。所有函数均为 ``async``，底层走
``role_store``。

末位保护（至少留 1 人）：``revoke_role`` / ``overwrite_role_members`` 在
删最后一人或清空时返回 ``last_one`` / ``empty``，由路由层映射 409。

种子（冷启动首批审批人）由 ``role_store.ensure_tables`` 负责，本模块
不再管理 JSON 文件。
"""
from __future__ import annotations

import logging

from bocomadp import role_store

logger = logging.getLogger("bocomadp.market_reviewers")

#: 审批人角色 key（与 role_store.REVIEWER_ROLE 保持一致）。
REVIEWER_ROLE = "reviewer"


async def is_reviewer(user_id: str) -> bool:
    """审批权判据：user_id 是否在 ``reviewer`` 角色内。"""
    return await role_store.has_role(user_id, REVIEWER_ROLE)


async def effective_reviewers() -> list[str]:
    """生效审批人名单（``reviewer`` 角色成员，排序返回）。"""
    return sorted(await role_store.list_role_members(REVIEWER_ROLE))


async def add_reviewers(user_ids: list[str]) -> int:
    """批量新增审批人（幂等，空白忽略）；返回实际新增人数。"""
    added = 0
    for raw in user_ids or []:
        added += await role_store.assign_role((raw or "").strip(), REVIEWER_ROLE)
    return added


async def overwrite_reviewers(user_ids: list[str]) -> str:
    """全量覆盖审批人名单（"清空重来"的显式出口）。

    返回 ``ok`` / ``empty``（清单为空或全空白——触发末位保护，路由层
    映射 409）。
    """
    return await role_store.overwrite_role_members(REVIEWER_ROLE, user_ids)


async def remove_reviewer(user_id: str) -> str:
    """删单个审批人：``not_found`` / ``last_one`` / ``ok``。"""
    return await role_store.revoke_role((user_id or "").strip(), REVIEWER_ROLE)


async def load_whitelist() -> None:
    """兼容旧启动钩子：改调 ``role_store.ensure_tables``（建表 + 种子）。"""
    await role_store.ensure_tables()


__all__ = [
    "REVIEWER_ROLE",
    "add_reviewers",
    "effective_reviewers",
    "is_reviewer",
    "load_whitelist",
    "overwrite_reviewers",
    "remove_reviewer",
]
