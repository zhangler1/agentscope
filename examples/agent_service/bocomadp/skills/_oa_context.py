# -*- coding: utf-8 -*-
"""skillhub 登录账号（OA）的解析 —— 请求头 ``oa``，与平台身份解耦。

背景：skillhub 的会话 cookie 由「本地 AES 凭证（明文 = ``<OA>#<ts>``）」登录
换取，因此需要一个 **OA 账号**。此前该账号直接取平台身份 ``X-User-ID``；
现在改为由前端在请求头 ``oa`` 里显式携带，两者职责分离：

- ``oa`` 头：**只**决定 skillhub 登录身份（读目录 / 我的上传 / 收藏 / 下载安装）
- ``X-User-ID``：仍然决定平台身份（资源归属、会话归属、workspace、日志 user_id）

解析规则集中在这里定义一次（头名、回退策略、严格模式），供各路由以
FastAPI 依赖注入：``oa: str = Depends(get_current_oa)``。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from agentscope.app.deps import get_current_user_id

from ._skillhub_auth import OA_SEPARATOR

#: 前端携带 OA 的请求头名（改这里即全局生效）。
OA_HEADER = "oa"

#: 缺 ``oa`` 头时是否直接拒绝。
#: ``False``（默认，灰度期）：回退 ``X-User-ID``，老调用方零破坏。
#: ``True``：返回 400，避免「看起来切了、其实还在用旧值」。
REQUIRE_OA = False


async def get_current_oa(
    oa: str | None = Header(
        default=None,
        alias=OA_HEADER,
        description="skillhub 登录账号（OA）；省略时回退 X-User-ID",
    ),
    user_id: str = Depends(get_current_user_id),
) -> str:
    """返回 skillhub 登录用的 OA 账号。

    优先级：请求头 ``oa`` → ``X-User-ID``。

    Args:
        oa (`str | None`): 请求头 ``oa``（HTTP 头名大小写不敏感）。
        user_id (`str`): 注入的平台身份（``X-User-ID``），作为回退值。

    Returns:
        `str`: 非空的 OA 账号。

    Raises:
        `HTTPException`:
            - 400：``REQUIRE_OA`` 为 ``True`` 且 ``oa`` 头缺失/空白。
            - 422：账号含分隔符 ``#``（AES 凭证明文用 ``#`` 分隔 OA 与
              时间戳，含它会导致登录凭证无法构造）。

    注意：这里只做「格式可用性」校验，不做权限判定——平台身份归 ``user_id``。
    """
    account = (oa or "").strip()
    if not account and REQUIRE_OA:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{OA_HEADER} header is required.",
        )
    account = account or user_id
    if OA_SEPARATOR in account:
        # 提前拦截：否则会一路带到 build_auth_token 抛 ValueError → 500
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{OA_HEADER} must not contain {OA_SEPARATOR!r}.",
        )
    return account


__all__ = ["OA_HEADER", "REQUIRE_OA", "get_current_oa"]
