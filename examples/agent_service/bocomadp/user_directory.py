# -*- coding: utf-8 -*-
"""OA 用户信息查询（ap_user 表）。

``ap_user`` 是行内 OA 的用户主数据表：主键为 ``USER_ID``，``LOGIN_NAME``
非唯一，同一登录名可能对应多条（不同 ``USER_ID``）记录，所以"按登录名查人"
天然是**多行**语义，本模块返回列表而不是单条。

表归属：ap_user 由 OA 侧维护，本模块**只读**——不建表、不写入、
不做迁移（表结构以 OA 的 DDL 为准，见 docs/user-info-api.md）。

连接：**ap_user 与业务表同库**，直接复用框架 ``AsyncSQLAlchemyStorage``
的连接池（``bind_storage(storage)`` 绑定，与 agents / role / team /
market 等表同库同池），不额外开连接池；未绑定时回退到按业务库
``config.db.url`` 自建的引擎（仅用于单测、脚本等没有框架 storage 的场景）。

状态过滤：ap_user.USER_STATUS 列存的是用户状态（0-失效 / 1-生效 /
2-待确认 / 3-休假 / 4-冻结），查询固定排除 ``USER_STATUS='0'``（失效），
其余状态（生效 / 待确认 / 休假 / 冻结）都返回。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from bocomadp.config import get_app_config

logger = logging.getLogger("bocomadp.user_directory")

#: ap_user.USER_STATUS 的"失效"取值（0-失效；其余状态都视为有效）
USER_STATUS_INACTIVE = "0"

#: 查询语句：只取对外需要的 5 列，条件为登录名精确匹配 + 状态为生效
_SELECT_SQL = (
    "SELECT USER_ID, ORG_ID, USER_CODE, LOGIN_NAME, USER_NAME "
    "FROM ap_user "
    "WHERE LOGIN_NAME = :login_name AND USER_STATUS != :user_status"
)

#: 框架 storage 的 engine（由 bind_storage 绑定，优先使用）
_engine: Any = None

#: 未绑定框架 storage 时自建的引擎（同一业务库，仅作回退）
_fallback_engine: Any = None


class UserInfoRow(BaseModel):
    """ap_user 的一行。

    字段名与表列名保持一致（USER_ID / ORG_ID / ...），便于与 OA 侧对账；
    USER_ID、ORG_ID 在 OA 侧是 decimal(10,0)，这里统一输出 int。
    """

    USER_ID: int | None = None
    ORG_ID: int | None = None
    USER_CODE: str | None = None
    LOGIN_NAME: str | None = None
    USER_NAME: str | None = None


def _to_int(value: Any) -> int | None:
    """把 USER_ID / ORG_ID 的 decimal 取值转成 int；空值或非法值返回 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def bind_storage(storage: Any) -> None:
    """绑定框架 ``AsyncSQLAlchemyStorage`` 的 engine，复用其连接池。

    ap_user 与业务表同库，因此不自己开连接：直接借用框架已建立的
    ``storage._engine``，与 agents / role / team / market 等表同池。
    与 ``role_store.bind_storage`` 同一范式。
    """
    global _engine
    engine = getattr(storage, "_engine", None)
    if engine is None:
        logger.warning("storage has no _engine; user_directory cannot bind")
        return
    _engine = engine
    logger.info("user_directory: 已复用框架 storage 连接池查询 ap_user")


async def _get_engine() -> Any:
    """取查询用的引擎：优先框架 storage 的池，未绑定时自建（同一业务库）。"""
    global _fallback_engine
    if _engine is not None:
        return _engine
    if _fallback_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        url = get_app_config().db.url
        _fallback_engine = create_async_engine(url, pool_pre_ping=True)
        # 只打印去掉账号密码的连接串，避免凭证进日志
        logger.info(
            "user_directory: 未绑定框架 storage，回退自建引擎 %s",
            url.rsplit("@", 1)[-1],
        )
    return _fallback_engine


async def query_users_by_login_name(
    login_name: str,
) -> list[UserInfoRow]:
    """按 OA 登录名查 ap_user，排除失效（USER_STATUS='0'）用户。

    Args:
        login_name (`str`): OA 登录名，对应 ap_user.LOGIN_NAME，
            **严格区分大小写**（大小写敏感），按 OA 值精准匹配。

    Returns:
        `list[UserInfoRow]`: 命中的用户列表，按 USER_ID 升序；
            没有命中返回空列表（不是报错）。
    """
    sql = _SELECT_SQL
    params: dict[str, Any] = {
        "login_name": login_name,
        "user_status": USER_STATUS_INACTIVE,
    }
    sql += " ORDER BY USER_ID ASC"

    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = (
            await conn.execute(text(sql), params)
        ).mappings().all()

    # 严格按登录名比对（区分大小写）：库列的排序规则不一定区分大小写，
    # 这里在取回后做精确匹配，保证"OA 值精准匹配"的语义不依赖数据库。
    result = []
    for r in rows:
        if r["LOGIN_NAME"] != login_name:
            continue
        result.append(
            UserInfoRow(
                USER_ID=_to_int(r["USER_ID"]),
                ORG_ID=_to_int(r["ORG_ID"]),
                USER_CODE=r["USER_CODE"],
                LOGIN_NAME=r["LOGIN_NAME"],
                USER_NAME=r["USER_NAME"],
            ),
        )
    return result


__all__ = [
    "USER_STATUS_INACTIVE",
    "UserInfoRow",
    "bind_storage",
    "query_users_by_login_name",
]
