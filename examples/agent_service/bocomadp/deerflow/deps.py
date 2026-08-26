# -*- coding: utf-8 -*-
"""deerflow 路由的 FastAPI 依赖注入（单例取自 app.state）。"""

from __future__ import annotations

from fastapi import Header, Request

from .bridge import BusBridge
from .runs import RunManager

DEFAULT_USER_ID = "jhzd"
"""X-User-ID 缺省用户：jx_chat 前端只注入 JUMP token、不携带
X-User-ID，请求统一落库到默认用户。"""


async def get_deerflow_user_id(
    x_user_id: str | None = Header(
        default=DEFAULT_USER_ID,
        description="Caller's user ID. Optional; missing or empty "
        "header falls back to 'jhzd' (jx_chat frontend sends no "
        "X-User-ID).",
    ),
) -> str:
    """Return the caller's user ID; missing/empty header → default 'jhzd'.

    jx_chat 前端只注入 JUMP token、不携带 X-User-ID，缺省落库到
    ``jhzd`` 用户（单租户部署）；显式携带时原样采用。
    """
    if x_user_id and x_user_id.strip():
        return x_user_id.strip()
    return DEFAULT_USER_ID


async def get_run_manager(request: Request) -> RunManager:
    """Return the application-wide :class:`RunManager`."""
    return request.app.state.run_manager


async def get_bridge(request: Request) -> BusBridge:
    """Return the application-wide :class:`BusBridge`."""
    return request.app.state.bus_bridge


__all__ = [
    "get_run_manager",
    "get_bridge",
    "get_deerflow_user_id",
]
