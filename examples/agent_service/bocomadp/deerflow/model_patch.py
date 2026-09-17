# -*- coding: utf-8 -*-
"""请求级模型参数补丁：包装框架 ``get_model``，合并 run_context。

框架 ``ChatService._run_impl`` 每轮 run 都经
``agentscope.app._service._chat.get_model`` 重建模型（无缓存），因此
请求级参数（``context.thinking_enabled`` / ``context.reasoning_effort``）
可以在模型构造后、agent 构建前合并进 ``Parameters``，实现"构建时配给
模型"的效果，无需 per-call 覆盖。

``context_size`` 也在同一位置用 ``model_registry`` 的**当次真值**覆盖（见
:func:`resolve_model_meta`）——它是压缩阈值的唯一输入，不允许陈旧。

不改框架源码：运行时替换 ``agentscope.app._service._chat`` 模块内的
``get_model`` 全局名（唯一调用点 ``_chat.py`` 的 ``_run_impl``），
无 import 顺序问题。
"""
from __future__ import annotations

import logging
from typing import Any

from bocomadp.routers.model_registry import resolve_model_meta

logger = logging.getLogger(__name__)

_original_get_model: Any = None


async def _patched_get_model(user_id: str, config: Any, access: Any):
    """调用原 get_model 后，先按模型库覆盖 context_size，再合并 run_context。"""
    from bocomadp.deerflow.run_context import get_run_context

    model = await _original_get_model(user_id, config, access)

    # 覆盖构造期从快照读到的 context_size：压缩阈值必须用库里的当次真值。
    # 必须早于下方两处 early return，否则"无 run_context"这条最常见路径会跳过。
    meta = await resolve_model_meta(model.model)
    if meta is not None:
        model.context_size = int(meta["context_size"])

    cfg = get_run_context()
    if not cfg:
        return model
    parameters = getattr(model, "parameters", None)
    if parameters is None:
        return model
    if cfg.get("thinking_enabled") is not None:
        parameters.enable_thinking = bool(cfg["thinking_enabled"])
    effort = cfg.get("reasoning_effort")
    if effort:
        parameters.reasoning_effort = str(effort)
    return model


def patch_get_model() -> None:
    """替换 chat service 的 ``get_model`` 绑定（幂等）。"""
    global _original_get_model
    if _original_get_model is not None:
        return

    from agentscope.app._service import _chat as _chat_module

    _original_get_model = _chat_module.get_model
    _chat_module.get_model = _patched_get_model
    logger.info(
        "patched %s.get_model with request-level run_context merge",
        _chat_module.__name__,
    )


__all__ = ["patch_get_model", "_patched_get_model"]
