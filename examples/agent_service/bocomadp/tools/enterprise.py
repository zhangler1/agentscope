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
from typing import Any

from agentscope.tool import FunctionTool, ToolBase

from ..deerflow.custom_params import get_custom_params
from ._naming import tool_name
from .contact_search import contact_search_tool
from .cross_search import (
    _current_agent_id as _cross_search_agent_id,
    _current_user_id as _cross_search_user_id,
    cross_search_tool,
)
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

#: 企业工具的中/英文名对（顺序：中文名、英文名）。
#: 用于 ``custom_params.usableTools`` 名单归一：名单条目无论写中文名还是
#: 英文名（``BOCOMADP_TOOL_ASCII_NAMES`` 任一形态）都能命中，并换算为
#: 当前运行时形态的工具名；也把豁免范围钉死在企业工具名空间内（名单里
#: 出现 Bash 等 builtins 名不会误豁免）。
_ENTERPRISE_NAME_PAIRS: tuple[tuple[str, str], ...] = (
    ("通讯录查询", "contact_search"),
    ("物理系统负责人查询", "physical_contact_search"),
    ("外数查", "raw_request_tool"),
    ("read_tool_result", "read_tool_result"),
    ("汇率查询", "exchange_rate"),
    ("利率查询", "interest_rate"),
    ("跨知识搜索", "cross_search"),
    ("行内搜索", "vector_search"),
    ("个人知识库搜索", "personal_search"),
    ("联网搜索", "online_search"),
    ("query_internal_doc", "query_internal_doc"),
    ("submit_it_ticket", "submit_it_ticket"),
)

#: 任一形态名（中/英文）→ 基准英文名的归一映射。
_NAME_TO_CANONICAL: dict[str, str] = {
    name: en for cn, en in _ENTERPRISE_NAME_PAIRS for name in (cn, en)
}

#: 基准英文名 → 中文名的反向映射（换算运行时名用）。
_CANONICAL_TO_CN: dict[str, str] = {
    en: cn for cn, en in _ENTERPRISE_NAME_PAIRS
}


def usable_enterprise_tool_names(usable: Any) -> set[str]:
    """把 ``custom_params.usableTools`` 名单换算为当前运行时形态的工具名。

    只保留命中企业工具名空间（中/英文任一形态）的条目，其余（builtins
    名、未知名等）静默忽略——保证"只作用于企业工具层"与白名单豁免
    范围不越界。

    Args:
        usable: ``custom_params.usableTools`` 原始值（list / None / 其他）。

    Returns:
        当前运行时形态（跟随 ``BOCOMADP_TOOL_ASCII_NAMES``）的工具名
        集合；非 list 或全无效条目 → 空集合。
    """
    if not isinstance(usable, list):
        return set()
    names: set[str] = set()
    for item in usable:
        canonical = _NAME_TO_CANONICAL.get(str(item).strip())
        if canonical:
            names.add(tool_name(_CANONICAL_TO_CN[canonical], canonical))
    return names


def usable_tool_names(usable: Any) -> set[str]:
    """把 ``custom_params.usableTools`` 名单换算为运行时形态的工具名集合。

    与 :func:`usable_enterprise_tool_names` 不同，本函数把名单里**未命中**
    企业工具名空间的条目原样保留（用于按名原样匹配项目工具等非企业工具），
    使 ``usableTools`` 管辖范围覆盖工具列表接口可见的全部工具（项目工具 +
    企业工具，不含 MCP / builtins / framework）。

    匹配规则：

    - 命中企业工具名空间（中/英文任一形态）的条目 → 经 :func:`tool_name`
      归一为当前运行时形态（跟随 ``BOCOMADP_TOOL_ASCII_NAMES``）；
    - 其余条目 → 原样保留（去除首尾空白），用于按工具名原样匹配项目工具
      （项目工具名固定 ASCII，无中英文之分；builtins / 未知名原样保留
      也不会误命中，因为项目工具里没有同名工具）。

    Args:
        usable: ``custom_params.usableTools`` 原始值（list / None / 其他）。

    Returns:
        运行时形态工具名集合；非 list / 空 list → 空集合。
    """
    if not isinstance(usable, list):
        return set()
    names: set[str] = set()
    for item in usable:
        s = str(item).strip()
        if not s:
            continue
        canonical = _NAME_TO_CANONICAL.get(s)
        if canonical:
            names.add(tool_name(_CANONICAL_TO_CN[canonical], canonical))
        else:
            names.add(s)
    return names


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

    ``usableTools`` 请求级名单（只作用于企业工具层，优先级高于 per-agent
    白名单）：

    - 缺失 / ``None`` / 非数组 / 空数组 → 不挂载任何企业工具（全禁用）；
    - 非空数组 → 只保留名单内的工具（中/英文名均可匹配）；名单只收缩、
      不扩张——开关关闭的工具即使列入名单也不会挂载。

    本函数在 run 任务内由框架 AgentToolFactory 调用，custom_params
    ContextVar 已随 ``asyncio.create_task`` 复制进来，可直接读取。
    """
    params = get_custom_params()
    logger.info(
        "build_enterprise_tools: session=%s agent=%s custom_params keys=%s "
        "usableTools=%r",
        session_id,
        agent_id,
        list(params.keys()),
        params.get("usableTools"),
    )
    tools: list[ToolBase] = [
        contact_search_tool,
        physical_contact_search_tool,
        # FunctionTool(query_employee_info, name="查询员工信息",is_read_only=True),
        FunctionTool(query_internal_doc, is_read_only=True),
        FunctionTool(submit_it_ticket),
        raw_request_tool,  # 已是 FunctionTool 实例（工具名"外数查"）
        read_tool_result_tool,  # 需状态注入,自定义 ToolBase(非 FunctionTool)
        exchange_rate_tool,   # 已是 FunctionTool 实例（工具名"汇率查询"）
        interest_rate_tool,   # 已是 FunctionTool 实例（工具名"利率查询"）
    ]

    # cross_search 始终挂载（2026-08-20 起不再受 vector_search_switch 控制）
    _cross_search_user_id.set(user_id)
    _cross_search_agent_id.set(agent_id)
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

    # usableTools 请求级名单过滤（只作用于企业工具层）：
    # - 缺失 / None / 非数组 / 空数组 → 不挂载任何企业工具（全禁用）；
    # - 非空数组 → 只保留名单内的工具（中/英文名均可匹配）。
    # 名单只收缩、不扩张：上方开关关闭的工具即使列入名单也不会挂载。
    usable = params.get("usableTools")
    if not isinstance(usable, list) or not usable:
        logger.info(
            "enterprise tools: usableTools missing or empty -> "
            "no enterprise tool mounted (session=%s)",
            session_id,
        )
        return []
    allowed = usable_enterprise_tool_names(usable)
    logger.info(
        "build_enterprise_tools: usableTools=%s allowed(normalized)=%s "
        "before_filter_names=%s (session=%s)",
        usable,
        sorted(allowed),
        [getattr(t, "name", "") for t in tools],
        session_id,
    )
    tools = [t for t in tools if getattr(t, "name", "") in allowed]
    logger.info(
        "build_enterprise_tools: RETURN names=%s (session=%s)",
        [getattr(t, "name", "") for t in tools],
        session_id,
    )

    return tools
