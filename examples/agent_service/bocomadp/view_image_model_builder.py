# -*- coding: utf-8 -*-
"""图片解析统一多模态模型：临时视觉模型构建。

与压缩模型（``bocomadp/summarization_model_builder.py``）同模式：

- 凭证按 ``user_id`` + ``credential_id`` 查库，二者由使用方在
  ``runtime_configs`` 表 ``view_image`` 配置提供，代码只按查库结果构建，
  不做凭证创建、也不从 ID 解析任何字段；
- 模型实例**不缓存、不共享**：每次图片解析临时构建、用后 ``aclose()``，
  消除 ``_api_key_override`` 等实例属性跨会话竞争，配置热更新即时生效；
- 与压缩模型唯一差异：必须显式使用 ``OpenAIChatFormatter`` ——
  主对话链路默认的 ``DeepSeekChatFormatter`` 硬编码不支持图片输入，
  视觉解析需要 OpenAI 兼容 formatter 才能携带图片 DataBlock。
"""
from __future__ import annotations

from typing import Any

from agentscope.credential import CredentialFactory
from agentscope.formatter import OpenAIChatFormatter

from bocomadp.providers.ellm_chat_model import EllmChatModel


def build_image_parse_model(
    credential_data: dict[str, Any],
    model_name: str,
) -> EllmChatModel:
    """按凭证记录与配置模型名构建临时多模态模型实例。

    Args:
        credential_data: ``storage.get_credential(...)`` 返回记录的 ``data``
            dict（含 api_key/base_url/scene_code/api_key_url 等）。
        model_name: 配置的 ELLM 多模态模型名（``view_image.model_name``）。

    Returns:
        ``EllmChatModel`` 实例（调用方负责用后 ``aclose()``）。
    """
    credential = CredentialFactory.from_dict(credential_data)
    model_cls = credential.get_chat_model_class()
    return model_cls(
        credential=credential,
        model=model_name,
        # 视觉解析必须用 OpenAI 兼容 formatter（DeepSeekChatFormatter
        # 不支持图片输入）；context_size 不传，由 EllmChatModel 按模型名
        # 从 Redis 默认读取（图片解析无压缩触发阈值语义）。
        formatter=OpenAIChatFormatter(),
    )


__all__ = [
    "build_image_parse_model",
]
