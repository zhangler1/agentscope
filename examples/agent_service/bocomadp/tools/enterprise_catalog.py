# -*- coding: utf-8 -*-
"""企业工具目录 —— 纳入可配置集合（M）的企业工具实例解析。

:mod:`bocomadp.tool_catalog` 只放纯常量（避免与 ``tools`` 包形成导入环），
这里负责把实际的企业工具实例解析成 ``name`` / ``description``。

名字一律取自工具实例的 ``.name``，因此与运行时保持一致：默认中文名，
设置 ``BOCOMADP_TOOL_ASCII_NAMES=1`` 时切换为 ASCII 名
（见 :mod:`bocomadp.tools._naming`）。
"""

from __future__ import annotations

import logging

from ..tool_catalog import ENTERPRISE_EXCLUDED_NAMES
from .contact_search import contact_search_tool
from .cross_search import cross_search_tool
from .exchange_rate import exchange_rate_tool
from .interest_rate import interest_rate_tool
from .personal_search import personal_search_tool
from .physical_contact_search import physical_contact_search_tool
from .raw_request import raw_request_tool
from .read_tool_result import read_tool_result_tool
from .vector_search import vector_search_tool

logger = logging.getLogger("bocomadp.enterprise_catalog")

#: 纳入可配置集合的企业工具实例（顺序即展示顺序）。
#: 联网搜索（online_search）与两个占位（query_internal_doc /
#: submit_it_ticket）按要求暂不纳入，故此处不引用。
_ENTERPRISE_TOOLS: tuple = (
    contact_search_tool,
    physical_contact_search_tool,
    raw_request_tool,
    read_tool_result_tool,
    exchange_rate_tool,
    interest_rate_tool,
    cross_search_tool,
    vector_search_tool,
    personal_search_tool,
)


def enterprise_tools_meta() -> list[dict[str, str]]:
    """企业工具的展示元数据（name + description），已排除不纳入的工具。

    Returns:
        list[dict[str, str]]: ``[{"name": ..., "description": ...}, ...]``
    """
    metas: list[dict[str, str]] = []
    for tool in _ENTERPRISE_TOOLS:
        name = getattr(tool, "name", "") or ""
        if not name or name in ENTERPRISE_EXCLUDED_NAMES:
            continue
        metas.append(
            {
                "name": name,
                "description": getattr(tool, "description", "") or "",
            },
        )
    return metas


def enterprise_tool_names() -> list[str]:
    """企业工具中纳入可配置集合的名字（已排除联网搜索与占位）。"""
    return [m["name"] for m in enterprise_tools_meta()]


__all__ = ["enterprise_tools_meta", "enterprise_tool_names"]
