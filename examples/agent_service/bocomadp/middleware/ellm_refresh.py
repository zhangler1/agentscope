# -*- coding: utf-8 -*-
"""ELLM key-refresh agent middleware.

每次模型调用前，通过 :class:`EllmKeyRefresher` 惰性检查/刷新 ELLM
apikey（过期判定 + ``MessageBus.acquire_lock`` 并发防抖 + 失败回落），
再用 ``EllmChatModel.set_api_key`` 把新鲜 key 注入到当前模型实例的
请求头（``Authorization: Bearer <key>``），并设置 ``inject_think_tag``
开关——优先级：请求体 ``custom_params.add_think``（deerflow run/stream
每轮携带）> Redis 模型表 ``bocomadp:model:think_tag``（按模型名）> 默认
False。不换类、不重建 client，模型调用链保持不变。

挂载方式（bocomadp main.py）::

    from bocomadp.middleware.ellm_refresh import build_ellm_refresh_middleware

    app = create_app(
        ...,
        extra_agent_middlewares=build_ellm_refresh_middleware(storage, message_bus),
    )
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

try:
    from agentscope.middleware import MiddlewareBase
except ImportError:  # pragma: no cover — offline syntax fallback
    class MiddlewareBase:  # type: ignore
        """Fallback MiddlewareBase for syntax checking."""

        _is_agent_middleware = True

        def is_implemented(self, hook_name: str) -> bool:
            base = getattr(MiddlewareBase, hook_name, None)
            sub = getattr(type(self), hook_name, None)
            return base is not sub

        async def list_tools(self) -> list:
            return []

        async def get_middleware_key(self) -> str:
            return self.__class__.__name__


MiddlewareBase._is_agent_middleware = True  # type: ignore[attr-defined]

from bocomadp.deerflow.custom_params import get_custom_params  # noqa: E402
from bocomadp.providers.ellm_chat_model import (  # noqa: E402
    EllmChatModel,
    _get_think_tag_from_redis,
)


def _parse_add_think(value: Any) -> bool | None:
    """严格解析请求级 ``add_think``，无法识别返回 ``None``。

    避免 ``bool("false")`` 为 True 的陷阱：仅接受 bool / 数字 /
    常见布尔字符串；``None`` 表示未指定或无法识别，由调用方回退
    下一优先级（Redis 模型表）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return None


class EllmKeyRefreshMiddleware(MiddlewareBase):
    """每次模型调用前把刷新后的 ELLM apikey 注入模型请求头。

    - 非 :class:`EllmChatModel` 模型直接透传，不做任何处理；
    - :class:`EllmChatModel` 模型：``EllmKeyRefresher`` 惰性刷新 →
      ``set_api_key`` 注入 → 设置 ``inject_think_tag``（请求体
      ``custom_params.add_think`` 优先，否则按模型名查 Redis 模型表）。
    """

    def __init__(
        self,
        storage: Any,
        message_bus: Any,
        user_id: str,
        refresh_ahead_secs: float = 0.0,
    ) -> None:
        from bocomadp.providers.ellm_key import EllmKeyRefresher

        self._refresher = EllmKeyRefresher(
            storage,
            message_bus,
            user_id,
            refresh_ahead_secs=refresh_ahead_secs,
        )

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        current_model = input_kwargs.get("current_model")

        if isinstance(current_model, EllmChatModel):
            credential_id = getattr(
                getattr(current_model, "credential", None),
                "id",
                None,
            )
            if credential_id:
                key, _ = await self._refresher.ensure_fresh_key(
                    credential_id,
                )
                current_model.set_api_key(key)
                # inject_think_tag 优先级：
                #   1) 请求体 custom_params.add_think（deerflow run/stream
                #      每轮携带，第一优先级）；
                #   2) Redis 模型表 bocomadp:model:think_tag（按模型名）；
                #   3) 默认 False。
                # 原生 /chat/ 或请求未携带 add_think 时，第 1 级返回
                # None，自动回退到第 2/3 级，行为与之前一致。
                req_think = _parse_add_think(
                    (get_custom_params() or {}).get("add_think"),
                )
                if req_think is not None:
                    current_model.inject_think_tag = req_think
                else:
                    current_model.inject_think_tag = (
                        await _get_think_tag_from_redis(current_model.model)
                    )
                # 401 时把该凭证的 key 置为过期（当前调用不重试，下一次
                # 使用该凭证的调用会走惰性刷新）。回调闭包绑定本次
                # credential_id，避免并发串号。
                current_model.set_auth_invalidate_callback(
                    lambda: self._refresher.invalidate_key(credential_id),
                )
                logger.debug(
                    "injected refreshed ELLM key (user=%s, credential=%s)",
                    self._refresher.user_id,
                    credential_id,
                )

        return await next_handler(**input_kwargs)


def build_ellm_refresh_middleware(
    storage: Any,
    message_bus: Any,
    refresh_ahead_secs: float = 0.0,
) -> Any:
    """构造 ``AgentMiddlewareFactory``，供 ``create_app(extra_agent_middlewares=...)`` 使用。

    Args:
        storage: StorageBase 实例（bocomadp main.py 已持有）。
        message_bus: MessageBus 实例。
        refresh_ahead_secs: ELLM key 提前刷新窗口（秒），来自
            ``config.ellm_key_refresh.refresh_ahead_secs``。

    Returns:
        ``AgentMiddlewareFactory`` —— ``async (user_id, agent_id, session_id)
        -> list[MiddlewareBase]``。
    """

    async def factory(
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list:
        return [
            EllmKeyRefreshMiddleware(
                storage,
                message_bus,
                user_id,
                refresh_ahead_secs=refresh_ahead_secs,
            )
        ]

    return factory


__all__ = ["EllmKeyRefreshMiddleware", "build_ellm_refresh_middleware"]
