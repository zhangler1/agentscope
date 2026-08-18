# -*- coding: utf-8 -*-
"""deerflow 路由的 FastAPI 依赖注入（单例取自 app.state）。"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from .bridge import BusBridge
from .runs import RunManager


async def get_deerflow_user_id(
    x_user_id: str | None = Header(
        default=None,
        description="Caller's user ID. Required (frontend adaptation "
        "dropped); missing or empty header → 401.",
    ),
) -> str:
    """Return the caller's user ID; missing/empty header → 401.

    Aligned with the native ``get_current_user_id``: the header is
    required, no fallback to a default user.
    """
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-User-ID header is required.",
        )
    return x_user_id.strip()


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
