# -*- coding: utf-8 -*-
"""企业中间件主动构建工厂（bocomadp）。

采用**主动 build** 而非 custom/ 被动扫描：
- 企业中间件由 :func:`build_enterprise_middlewares` 显式构建，
  每次 agent 组装时按会话创建独立实例（user_id / session_id 直传）；
- 由 ``main.py`` 的通用中间件构建入口（``build_agent_middlewares``）调用，
  与 ``MiddlewareRegistry`` 自动扫描的内置中间件合并注入。
"""
from __future__ import annotations

import os

from agentscope.middleware import MiddlewareBase

from .custom_prompt import CustomPromptMiddleware
from .slot_release import SlotReleaseMiddleware


async def build_enterprise_middlewares(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[MiddlewareBase]:
    """按配置返回当前会话需要的企业中间件列表。

    被 AgentScope 在每次 agent 组装时调用一次，因此可以在这里
    根据用户/会话返回不同的中间件组合。

    CustomPromptMiddleware 无 per-session 状态（提示词从请求级
    ContextVar 读取），每次组装构建新实例即可，无额外成本。

    SlotReleaseMiddleware 负责 run 结束无条件软释放温池 slot（配合
    SharedPvcK8sWorkspace.release_slot），开关
    ``ADP_K8S_SLOT_RELEASE_ON_RUN_END``（默认开）控制。
    参数签名（user_id / agent_id / session_id）为与 main.py 调用
    契约对齐而保留。
    """
    return [
        CustomPromptMiddleware(),
    ]

    if get_audit_config().enabled:
        middlewares.append(
            AuditMiddleware(user_id=user_id, session_id=session_id),
        )

    if os.getenv("ADP_K8S_SLOT_RELEASE_ON_RUN_END", "1") == "1":
        middlewares.append(
            SlotReleaseMiddleware(session_id=session_id),
        )

    return middlewares
