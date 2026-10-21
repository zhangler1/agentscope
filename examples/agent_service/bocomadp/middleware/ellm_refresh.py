# -*- coding: utf-8 -*-
"""ELLM key-refresh agent middleware.

每次模型调用前，通过 :class:`EllmKeyRefresher` 惰性检查/刷新 ELLM
apikey（过期判定 + ``MessageBus.acquire_lock`` 并发防抖 + 失败回落），
再用 ``EllmChatModel.set_api_key`` 把新鲜 key 注入到当前模型实例的
请求头（``Authorization: Bearer <key>``），并设置 ``inject_think_tag``
开关——优先级：请求体 ``custom_params.add_think``（deerflow run/stream
每轮携带）> 模型库 ``model_registry``（按模型名）> 默认
False。不换类、不重建 client，模型调用链保持不变。

另外实现 ``on_compress_context``：上下文压缩直接调用 ``agent.model``，
不经过 ``on_model_call`` 链，因此在压缩前补一次惰性刷新，避免长时间
空闲后 key 已过期时压缩请求 401。

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
from bocomadp.providers.ellm_chat_model import EllmChatModel  # noqa: E402
from bocomadp.routers.model_registry import (  # noqa: E402
    resolve_model_meta,
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
      ``custom_params.add_think`` 优先，否则按模型名查模型库）；
      并注入 401 回调：强制刷新 key 并重试当前调用一次，刷新失败则
      标记凭证 key 过期。
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

    @staticmethod
    def _credential_id(model: EllmChatModel) -> str | None:
        """模型绑定的凭证 id；缺失时返回 ``None``。"""
        return getattr(getattr(model, "credential", None), "id", None)

    async def _refresh_and_inject_key(
        self,
        model: EllmChatModel,
        credential_id: str,
    ) -> None:
        """惰性刷新 key 并写入模型的请求级 override。

        与 ``on_model_call`` 一致：刷新失败会向上抛出，由调用方决定
        是否吞掉（压缩路径吞掉，常规调用路径不吞）。
        """
        key, _ = await self._refresher.ensure_fresh_key(credential_id)
        model.set_api_key(key)

    def _install_auth_callbacks(
        self,
        model: EllmChatModel,
        credential_id: str,
    ) -> None:
        """安装 401 双回调（强制换新重试 / 刷新失败置过期）。

        两个闭包都绑定本次 ``credential_id``，避免并发串号。
        """
        model.set_refresh_key_callback(
            lambda: self._refresher.force_refresh_key(credential_id),
        )
        model.set_auth_invalidate_callback(
            lambda: self._refresher.invalidate_key(credential_id),
        )

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        current_model = input_kwargs.get("current_model")

        if isinstance(current_model, EllmChatModel):
            credential_id = self._credential_id(current_model)
            if credential_id:
                await self._refresh_and_inject_key(
                    current_model,
                    credential_id,
                )
                # inject_think_tag 优先级：
                #   1) 请求体 custom_params.add_think（deerflow run/stream
                #      每轮携带，第一优先级）；
                #   2) 模型库 model_registry（按模型名，直读 DB；DB 异常时
                #      回退快照，见 resolve_model_meta）；
                #   3) 默认 False。
                # 原生 /chat/ 或请求未携带 add_think 时，第 1 级返回
                # None，自动回退到第 2/3 级，行为与之前一致。
                req_think = _parse_add_think(
                    (get_custom_params() or {}).get("add_think"),
                )
                if req_think is not None:
                    current_model.inject_think_tag = req_think
                else:
                    meta = await resolve_model_meta(current_model.model)
                    current_model.inject_think_tag = (
                        bool(meta["think_tag"]) if meta else False
                    )
                self._install_auth_callbacks(current_model, credential_id)
                logger.debug(
                    "injected refreshed ELLM key (user=%s, credential=%s)",
                    self._refresher.user_id,
                    credential_id,
                )

        return await next_handler(**input_kwargs)

    async def on_compress_context(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        """压缩前刷新 ELLM key（压缩调用不走 ``on_model_call`` 链）。

        框架压缩在每轮 reasoning 前执行，直接调用 ``agent.model``；
        不在这里补刷新的话，长时间空闲后 key 过期时压缩请求会 401。

        刷新是 best-effort：失败只记 warning，不阻断压缩本身。
        401 回调先于刷新安装，因此即便主动刷新失败，真正遇到 401 时
        仍能强制换新并重试一次。
        """
        # input_kwargs 只含 context_config / instructions，这里用不到。
        del input_kwargs

        current_model = getattr(agent, "model", None)
        if isinstance(current_model, EllmChatModel):
            credential_id = self._credential_id(current_model)
            if credential_id:
                self._install_auth_callbacks(current_model, credential_id)
                try:
                    await self._refresh_and_inject_key(
                        current_model,
                        credential_id,
                    )
                except Exception as exc:  # noqa: BLE001 —— 刷新失败不阻断压缩
                    logger.warning(
                        "compress: pre-compression ELLM key refresh failed "
                        "(user=%s, credential=%s, error=%s)",
                        self._refresher.user_id,
                        credential_id,
                        exc,
                    )

        return await next_handler()


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
