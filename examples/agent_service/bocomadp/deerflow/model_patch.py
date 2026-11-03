# -*- coding: utf-8 -*-
"""请求级模型参数补丁：包装框架 ``get_model``，合并 run_context。

框架 ``ChatService._run_impl`` 每轮 run 都经
``agentscope.app._service._chat.get_model`` 重建模型（无缓存），因此
请求级参数（``context.thinking_enabled`` / ``context.reasoning_effort``）
可以在模型构造后、agent 构建前合并进 ``Parameters``，实现"构建时配给
模型"的效果，无需 per-call 覆盖。

``<think>`` 注入开关（``EllmChatModel.inject_think_tag``）也在同一位置
按**会话级**依据定下（见 :func:`_resolve_think_injection`）：注入是思考
模式的外部表现，二者不应各配一个开关，否则会出现"关思考却注入了开
标签、标签不闭合导致整段回答被当思考"这类错配。

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


def _resolve_think_injection(
    session_parameters: dict[str, Any],
    run_context: dict[str, Any],
    meta: dict[str, Any] | None,
) -> bool | None:
    """会话级 ``<think>`` 注入判定；``None`` = 未决（交回中间件）。

    优先级（方案 C）：

    1. 显式 ``add_think``：会话 ``chat_model_config.parameters``；请求级
       ``custom_params.add_think`` 由中间件在每次调用前覆盖，优先级更高；
    2. 派生：模型库 ``think_tag`` **且** 本会话开启思考
       （``run_context.thinking_enabled`` 优先，其次会话配置
       ``enable_thinking``）；
    3. 未决：两个来源都没给，保持存量行为（模型库 ``think_tag`` 决定）。

    Args:
        session_parameters (`dict[str, Any]`):
            会话 ``chat_model_config.parameters`` 原样内容。
        run_context (`dict[str, Any]`):
            请求级 run 配置（``thinking_enabled`` 等）。
        meta (`dict[str, Any] | None`):
            ``model_registry`` 中该模型的行；``None`` = 库中无此模型。

    Returns:
        `bool | None`: 注入开关；``None`` 表示未定。
    """
    # 与中间件共用同一严格解析（避免 bool("false") is True）；延迟导入
    # 避免"模型构建模块 → 中间件模块"的顶层循环依赖。
    from bocomadp.middleware.ellm_refresh import _parse_add_think

    explicit = _parse_add_think(session_parameters.get("add_think"))
    if explicit is not None:
        return explicit

    enabled = _parse_add_think(run_context.get("thinking_enabled"))
    if enabled is None:
        enabled = _parse_add_think(session_parameters.get("enable_thinking"))
    if enabled is None:
        return None

    needs_tag = bool(meta.get("think_tag")) if meta else False
    return needs_tag and enabled


def _apply_think_injection(
    model: Any,
    session_parameters: dict[str, Any],
    run_context: dict[str, Any],
    meta: dict[str, Any] | None,
) -> None:
    """把会话级判定写回模型；非 ELLM 模型 / 未决时不写。"""
    if not hasattr(model, "inject_think_tag"):
        return
    decided = _resolve_think_injection(session_parameters, run_context, meta)
    if decided is not None:
        model.inject_think_tag = decided


async def _patched_get_model(user_id: str, config: Any, access: Any):
    """调用原 get_model 后，先按模型库覆盖 context_size，再合并 run_context。

    ``<think>`` 注入判定放在两处 early return 之前：原生 ``/chat/`` 路径
    没有 run_context，若跟着 run_context 一起跳过，会话级的思考开关就
    永远传不到注入开关上。
    """
    from bocomadp.deerflow.run_context import get_run_context

    model = await _original_get_model(user_id, config, access)

    # 覆盖构造期从快照读到的 context_size：压缩阈值必须用库里的当次真值。
    # 必须早于下方两处 early return，否则"无 run_context"这条最常见路径会跳过。
    meta = await resolve_model_meta(model.model)
    if meta is not None:
        model.context_size = int(meta["context_size"])

    cfg = get_run_context()
    _apply_think_injection(
        model,
        session_parameters=getattr(config, "parameters", None) or {},
        run_context=cfg,
        meta=meta,
    )

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
