# -*- coding: utf-8 -*-
"""ELLM 供应商凭证。

自研/自部署的 ELLM 平台（模型由 **DeepSeek-V4-Flash** 提供），
对外暴露 OpenAI 兼容端点（``/v1``），因此：

- ``base_url`` 填 ELLM 部署的 OpenAI 兼容地址（如
  ``http://host.docker.internal:8001/v1``）；
- 聊天模型直接复用官方的 :class:`OpenAIChatModel`（``get_chat_model_class``）；
- 凭证**不绑定模型**：运行时的模型名由 agent/会话配置
  ``chat_model_config.model`` 提供，候选模型见随包分发的
  ``providers/_models/*.yaml``（``EllmChatModel.list_models``）。
"""
from __future__ import annotations

from typing import Literal, Type

from pydantic import ConfigDict, Field, SecretStr

from agentscope.credential import CredentialBase
from agentscope.model import ChatModelBase
from providers.ellm_chat_model import EllmChatModel


class ELLMCredential(CredentialBase):
    """ELLM 自研供应商凭证。"""

    model_config = ConfigDict(
        title="ELLM",
    )

    type: Literal["bocom_ellm_credential"] = "bocom_ellm_credential"
    """凭证类型标识（唯一，Pydantic discriminator 使用）。"""

    api_key: SecretStr = Field(
        default=SecretStr("sk-xxx"),
        description=(
            "ELLM 服务的 API key。本地部署通常不校验，省略时默认占位值 "
            "'sk-xxx'；运行时由 EllmKeyRefresher 注入真实 key，故占位即可。"
        ),
    )
    """API key（省略时使用默认占位值）。"""

    base_url: str = Field(
        description=(
            "ELLM 的 OpenAI 兼容端点（以 /v1 结尾）。容器内访问宿主机服务 "
            "用 host.docker.internal。"
        ),
    )
    """自定义 base URL（OpenAI 兼容端点）。"""

    organization: str | None = Field(
        default=None,
        description=(
            "组织 ID——OpenAIChatModel 构造时会读取该字段，必须存在。"
        ),
    )
    """组织 ID（官方 OpenAIChatModel 需要读取的字段）。"""

    scene_code: str = Field(
        description="场景编码（业务字段，前端传入，原样存储）。",
    )
    """场景编码。"""

    api_key_url: str = Field(
        description="API key 地址（业务字段，前端传入，原样存储）。",
    )
    """API key 地址。"""

    apikey_expires_at: float | None = Field(
        default=None,
        description="API key 过期时间（业务字段，前端可传空，原样存储）。",
    )
    """API key 过期时间。"""

    @classmethod
    def get_chat_model_class(cls) -> Type[ChatModelBase]:
        """ELLM 是 OpenAI 兼容接口——基于本包的 :class:`EllmChatModel`。

        子类化使 :meth:`list_models` 读取本包 ``providers/_models/*.yaml``
        候选卡（而不是官方 OpenAI 的候选）。
        """
        return EllmChatModel
