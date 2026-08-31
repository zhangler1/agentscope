# -*- coding: utf-8 -*-
"""行内模型平台（providers）。

面向 SDK 原生 ``chat()`` 接口的完整行内模型平台，模块构成：

- :mod:`providers.credential` —— ELLM 供应商凭证（导入即注册，幂等）；
- :mod:`providers.middleware` —— 每次模型调用前刷新并注入 ELLM apikey，
  设置 ``inject_think_tag``（优先级：会话级覆盖 > Redis 模型表 > 默认）；
- :mod:`providers.routers` —— 模型候选管理、模型凭证、会话级 think-tag
  覆盖等 HTTP 端点；
- :mod:`providers.ellm_chat_model` —— ELLM 模型实现（OpenAI 兼容网关）；
- :mod:`providers.ellm_key` —— ELLM apikey 生命周期（惰性刷新/强制刷新）。

运行参数统一来自 :mod:`config`（环境变量驱动）。
"""
from .credential import ELLMCredential
from .ellm_chat_model import EllmChatModel
from .ellm_key import EllmKeyRefresher, fetch_ellm_key
from .middleware import build_ellm_refresh_middleware
from .routers import (
    credential_model_router,
    ellm_models_router,
    session_think_tag_router,
)

__all__ = [
    "ELLMCredential",
    "EllmChatModel",
    "EllmKeyRefresher",
    "fetch_ellm_key",
    "build_ellm_refresh_middleware",
    "ellm_models_router",
    "credential_model_router",
    "session_think_tag_router",
]
