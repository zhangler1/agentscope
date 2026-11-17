# -*- coding: utf-8 -*-
"""OA 用户信息查询接口。

GET /users/query?oa=<登录名>

按 OA 登录名（对应 ap_user.LOGIN_NAME）查行内用户主数据，返回
USER_ID / ORG_ID / USER_CODE / LOGIN_NAME / USER_NAME 五项。

只返回**生效**用户（USER_STATUS='1'），其余状态（失效/待确认/休假/冻结）
直接被过滤掉。LOGIN_NAME 非唯一，同一登录名可能对应多条（不同 USER_ID），
故返回 items 列表。没有命中时返回 total=0、items=[]（不是 404）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from agentscope.app.deps import get_current_user_id

from bocomadp.user_directory import query_users_by_login_name

logger = logging.getLogger("bocomadp.user_info")

user_info_router = APIRouter(
    prefix="/users",
    tags=["user-info"],
)

#: OA 登录名最大长度（与 ap_user.LOGIN_NAME 列长一致）
MAX_LOGIN_NAME_LEN = 30


def _pick_login_name(
    request: Request,
    oa: str | None,
    OA: str | None,
) -> str:
    """取 OA 登录名：``oa`` 与 ``OA`` 两种写法都认。

    查询参数名默认是**大小写敏感**的（FastAPI 精确匹配 query key），
    所以这里显式声明两个参数名，再兜底一层任意大小写变体
    （``Oa`` / ``oA`` ...），前端拼错大小写不会直接 422。
    """
    for candidate in (oa, OA):
        if candidate and candidate.strip():
            return candidate.strip()
    for key, value in request.query_params.items():
        if key.lower() == "oa" and value and value.strip():
            return value.strip()
    return ""


@user_info_router.get(
    "/query",
    summary="按 OA 登录名查询用户信息",
)
async def query_user_info(
    request: Request,
    oa: str | None = Query(
        default=None,
        max_length=MAX_LOGIN_NAME_LEN,
        description="OA 登录名，对应 ap_user.LOGIN_NAME；与 OA 等价，二者必填其一",
    ),
    OA: str | None = Query(
        default=None,
        max_length=MAX_LOGIN_NAME_LEN,
        description="OA 登录名（大写写法，与 oa 等价）",
    ),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """查询 OA 用户主数据。"""
    login_name = _pick_login_name(request, oa, OA)
    if not login_name:
        raise HTTPException(status_code=422, detail="oa (or OA) is required")
    if len(login_name) > MAX_LOGIN_NAME_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"oa must be at most {MAX_LOGIN_NAME_LEN} characters",
        )

    try:
        rows = await query_users_by_login_name(login_name)
    except SQLAlchemyError as exc:
        logger.error(
            "查询 ap_user 失败 (oa=%s): %s",
            login_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"用户信息查询失败（{exc.__class__.__name__}）",
        ) from exc

    logger.info(
        "user_info: oa=%s caller=%s -> %d row(s)",
        login_name,
        user_id,
        len(rows),
    )
    return {
        "total": len(rows),
        "items": [row.model_dump() for row in rows],
    }


__all__ = ["user_info_router"]
