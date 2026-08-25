# -*- coding: utf-8 -*-
"""上下文压缩统一模型：临时压缩模型构建。

设计决策（见 docs/superpowers/specs/2026-08-21-context-compression-unified-model-design.md，
及统一配置 PG 化设计 2026-08-24-runtime-configs-pg-design.md）：

- 凭证按 ``user_id`` + ``credential_id`` 查库，二者由使用方在 config.yaml
  提供，代码只按查库结果构建，不做凭证创建、也不从 ID 解析任何字段；
- 压缩模型实例**不缓存、不共享**：每次压缩临时构建、用后 ``aclose()``，
  消除 ``_api_key_override`` 等实例属性跨会话竞争；
- ``context_size = min(会话模型.context_size, 压缩模型真实窗口)``：压缩调用必然
  装进压缩模型窗口，且大上下文会话的触发阈值不被压缩模型窗口拉低。
"""
from __future__ import annotations

from typing import Any

from agentscope.credential import CredentialFactory

from bocomadp.providers.ellm_chat_model import EllmChatModel, _get_model_context_size


def effective_context_size(session_context_size: int, model_name: str) -> int:
    """压缩模型实例的 context_size：min(会话模型, 压缩模型真实窗口)。"""
    return min(session_context_size, _get_model_context_size(model_name))


def build_summarization_model(
    credential_data: dict[str, Any],
    model_name: str,
    session_context_size: int,
) -> EllmChatModel:
    """按凭证记录与配置模型名构建临时压缩模型实例。

    Args:
        credential_data: ``storage.get_credential(...)`` 返回记录的 ``data``
            dict（含 api_key/base_url/scene_code/api_key_url 等）。
        model_name: 配置的 ELLM 模型名（``summarization.model_name``）。
        session_context_size: 当前会话模型（``agent.model.context_size``）。

    Returns:
        ``EllmChatModel`` 实例（调用方负责用后 ``aclose()``）。
    """
    credential = CredentialFactory.from_dict(credential_data)
    model_cls = credential.get_chat_model_class()
    return model_cls(
        credential=credential,
        model=model_name,
        context_size=effective_context_size(session_context_size, model_name),
    )


__all__ = [
    "effective_context_size",
    "build_summarization_model",
]
