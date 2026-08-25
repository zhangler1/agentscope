# -*- coding: utf-8 -*-
"""上下文压缩统一模型中间件。

挂载在 ``main.py`` 的 ``_build_agent_middlewares_with_ellm`` 工厂内，框架
``on_compress_context`` 钩子在所有会话（deerflow / 智能体工厂 / 专家团子会话）
压缩触发时执行：

1. 配置从 PG ``runtime_configs`` 表按 ``summarization`` 读取（真源，可热生效）；
   DB 无记录 / 读失败 → 视为未启用，纯透传（压缩用会话自身模型）；
2. 临时构建压缩模型实例（``context_size=min`` 语义）；
3. key 刷新 + 注入（压缩调用不走 ``on_model_call`` 链，必须在此调
   ``ensure_fresh_key`` 惰性刷新，key 有效则零开销）+ 安装 401 双回调兜底；
4. swap ``agent.model`` 执行框架原压缩逻辑；异常 → 恢复会话模型重试一次；
5. ``finally`` 恢复模型并 ``aclose()`` 释放连接池。
"""
from __future__ import annotations

import logging
from typing import Any

from agentscope.middleware import MiddlewareBase

from bocomadp.config.app_config import SummarizationConfig
from bocomadp.providers.ellm_chat_model import EllmChatModel
from bocomadp.providers.ellm_key import EllmKeyRefresher
from bocomadp.runtime_config_store import get_typed_config
from bocomadp.summarization_model_builder import build_summarization_model

logger = logging.getLogger(__name__)


class SummarizationMiddleware(MiddlewareBase):
    """压缩时把 ``agent.model`` 临时替换为统一压缩模型。

    配置运行时从 PG 读取，不缓存于构造期；user_id / credential_id / model_name
    均可经 CRUD API 热更新。
    """

    def __init__(
        self,
        storage: Any,
        message_bus: Any,
    ) -> None:
        self._storage = storage
        self._message_bus = message_bus

    async def on_compress_context(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> None:
        # 配置真源在 PG；无记录 / 读失败视为未启用，透传（压缩用会话自身模型）。
        cfg = await get_typed_config("summarization", SummarizationConfig)
        if cfg is None or not (
            cfg.enabled and cfg.user_id and cfg.credential_id and cfg.model_name
        ):
            return await next_handler()

        record = await self._storage.get_credential(cfg.user_id, cfg.credential_id)
        if record is None:
            logger.warning(
                "summarization: credential %r not found for user %r; "
                "fall back to session model",
                cfg.credential_id,
                cfg.user_id,
            )
            return await next_handler()

        model = build_summarization_model(
            record.data,
            cfg.model_name,
            agent.model.context_size,
        )

        # 压缩调用不走 on_model_call 链，必须在此主动保证 key 新鲜：
        # ensure_fresh_key 惰性刷新 —— key 有效直接复用（零开销），
        # 已过期则调网关换新并 upsert 写回存储，压缩一开始就用新 key。
        # 401 双回调仍保留作兜底（见 providers/ellm_chat_model.py）。
        # refresher 按当前 user_id 现场构造（轻量无状态，用后即弃），
        # 避免因 CRUD 热更新 user_id 后仍使用旧实例。
        if isinstance(model, EllmChatModel):
            refresher = EllmKeyRefresher(
                self._storage,
                self._message_bus,
                cfg.user_id,
            )
            credential_id = cfg.credential_id
            key, _ = await refresher.ensure_fresh_key(credential_id)
            model.set_api_key(key)
            model.set_refresh_key_callback(
                lambda: refresher.force_refresh_key(credential_id),
            )
            model.set_auth_invalidate_callback(
                lambda: refresher.invalidate_key(credential_id),
            )

        old_model = agent.model
        agent.model = model
        try:
            return await next_handler()
        except Exception:
            # 统一模型压缩失败 → 恢复会话模型完整重跑一次
            logger.warning(
                "summarization: unified model failed, retry with session model",
                exc_info=True,
            )
            agent.model = old_model
            return await next_handler()
        finally:
            agent.model = old_model
            await model.aclose()
