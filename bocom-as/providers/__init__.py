# -*- coding: utf-8 -*-
"""行内模型平台（providers）。

面向 SDK 原生 ``chat()`` 接口的完整行内模型平台，模块构成：

- :mod:`providers.credential` —— ELLM 供应商凭证（导入即注册，幂等）；
- :mod:`providers.middleware` —— 每次模型调用前刷新并注入 ELLM apikey
  （可选，401 自动强刷/重试）；
- :mod:`providers.routers` —— 模型凭证配置查询/部分更新等 HTTP 端点
  （服务端形态）；
- :mod:`providers.ellm_chat_model` —— ELLM 模型实现（OpenAI 兼容网关，
  模型名/元数据由构造参数或随包内置 ``_models/*.yaml`` 提供，不依赖
  Redis）；
- :mod:`providers.ellm_key` —— ELLM apikey 生命周期（惰性刷新/强制刷新）。

运行所需参数均由调用方显式传入（构造参数 / 中间件工厂参数）。
"""
from .credential import ELLMCredential
from .ellm_chat_model import EllmChatModel
from .ellm_key import EllmKeyRefresher, fetch_ellm_key
from .middleware import build_ellm_refresh_middleware
from .routers import credential_model_router

__all__ = [
    "ELLMCredential",
    "EllmChatModel",
    "EllmKeyRefresher",
    "fetch_ellm_key",
    "build_ellm_refresh_middleware",
    "credential_model_router",
]
