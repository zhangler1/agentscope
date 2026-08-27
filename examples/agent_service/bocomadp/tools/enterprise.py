# -*- coding: utf-8 -*-
"""企业工具主动构建工厂（bocomadp）。

采用**主动 build** 而非 custom/ 被动扫描：
- 企业工具属于确定性注入组件，由 :func:`build_enterprise_tools` 显式构建，
  每会话按需装配，行为可控、可观测；
- ``FunctionTool`` 显式包装保留 ``is_read_only`` 语义（查询类工具只读）；
- 由 ``main.py`` 的通用工具构建入口（``build_agent_tools``）调用，
  与 ``ToolRegistry`` 自动扫描的内置工具合并注入。
"""
from __future__ import annotations

import logging

from agentscope.tool import FunctionTool, ToolBase

from ..deerflow.custom_params import get_custom_params
from .contact_search import contact_search_tool
from .cross_search import cross_search_tool  # 已是 FunctionTool 实例（带注入中间件）
from .exchange_rate import exchange_rate_tool
from .interest_rate import interest_rate_tool
from .online_search import online_search_tool
from .personal_search import personal_search_tool
from .physical_contact_search import physical_contact_search_tool
from .placeholder import (
    query_internal_doc,
    submit_it_ticket,
)
from .raw_request import raw_request_tool
from .read_tool_result import read_tool_result_tool
from .vector_search import vector_search_tool

logger = logging.getLogger(__name__)


async def build_enterprise_tools(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[ToolBase]:
    """返回当前会话可用的企业内部工具。

    可在此根据 user_id / agent_id 做差异化授权：
    例如某些工具只对特定部门开放。

    检索开关（对齐 deer-flow custom_params，显式才生效）：

    - ``cross_search`` 始终挂载（2026-08-20 起不再受 vector_search_switch
      控制）。
    - ``physical_contact_search`` 始终挂载（物理系统负责人查询）。
    - ``vector_search_switch`` 显式 ``False`` → 不挂载行内搜索工具
      （vector_search）；未传 / ``True`` 保持默认挂载。
    - ``online_search_switch`` 显式 ``True`` → 挂载联网搜索工具
      （online_search）；默认不挂。
    - ``personal_search_switch`` 显式 ``True`` 且 ``tools_param`` 的
      ``personalKnowledgeSearch`` 空间参数（psnlSpaceCodeId /
      psnlCategoryIdList）齐备 → 挂载个人知识库搜索工具（personal_search）。

    本函数在 run 任务内由框架 AgentToolFactory 调用，custom_params
    ContextVar 已随 ``asyncio.create_task`` 复制进来，可直接读取。
    """
    params = get_custom_params()
    tools: list[ToolBase] = [
        contact_search_tool,
        physical_contact_search_tool,
        FunctionTool(query_internal_doc, is_read_only=True),
        FunctionTool(submit_it_ticket),
        raw_request_tool,  # 已是 FunctionTool 实例（工具名"外数查"）
        read_tool_result_tool,  # 需状态注入,自定义 ToolBase(非 FunctionTool)
        exchange_rate_tool,   # 已是 FunctionTool 实例（工具名"汇率查询"）
        interest_rate_tool,   # 已是 FunctionTool 实例（工具名"利率查询"）
    ]

    # cross_search 始终挂载（2026-08-20 起不再受 vector_search_switch 控制）
    tools.append(cross_search_tool)

    # vector_search_switch 显式 False → 不挂行内搜索；未传 / True 保持默认挂载
    vector_switch = params.get("vector_search_switch")
    if vector_switch is False:
        logger.info(
            "enterprise tools: vector_search disabled by "
            "vector_search_switch=false (session=%s)",
            session_id,
        )
    else:
        tools.append(vector_search_tool)

    # online_search_switch 显式 True → 挂联网搜索（默认不挂）
    if params.get("online_search_switch") is True:
        tools.append(online_search_tool)
    else:
        logger.debug(
            "enterprise tools: online_search skipped "
            "(online_search_switch != true, session=%s)",
            session_id,
        )

    # personal_search_switch 显式 True 且空间参数齐备 → 挂个人知识库搜索
    pks = (params.get("tools_param") or {}).get("personalKnowledgeSearch") or {}
    if (
        params.get("personal_search_switch") is True
        and pks.get("psnlSpaceCodeId")
        and pks.get("psnlCategoryIdList")
    ):
        tools.append(personal_search_tool)
    else:
        logger.debug(
            "enterprise tools: personal_search skipped "
            "(personal_search_switch != true or space params missing, "
            "session=%s)",
            session_id,
        )

    return tools
