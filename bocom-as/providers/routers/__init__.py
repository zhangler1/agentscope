# -*- coding: utf-8 -*-
"""行内模型平台 HTTP 路由。"""
from .credential_model import credential_model_router
from .ellm_models import ellm_models_router
from .session_think_tag import session_think_tag_router

__all__ = [
    "ellm_models_router",
    "credential_model_router",
    "session_think_tag_router",
]
