# -*- coding: utf-8 -*-
"""enterprise.py 挂载开关测试。"""
from __future__ import annotations

import asyncio

from bocomadp.deerflow.custom_params import (
    reset_custom_params,
    set_custom_params,
)
from bocomadp.middleware.factory import build_enterprise_middlewares
from bocomadp.tools._naming import tool_name
from bocomadp.tools.enterprise import build_enterprise_tools

# 工具名默认中文，设置 BOCOMADP_TOOL_ASCII_NAMES=1 后为 ASCII；
# 断言用 tool_name(...) 计算期望值，避免与开关耦合。
_CROSS = tool_name("跨知识搜索", "cross_search")
_VECTOR = tool_name("行内搜索", "vector_search")
_ONLINE = tool_name("联网搜索", "online_search")
_PERSONAL = tool_name("个人知识库搜索", "personal_search")


def _mount(params: dict | None) -> set[str]:
    token = set_custom_params(params or {})
    try:
        tools = asyncio.run(
            build_enterprise_tools("u1", "a1", "s1")
        )
        return {t.name for t in tools}
    finally:
        reset_custom_params(token)


def test_default_mounts_cross_and_vector():
    names = _mount({})
    assert _CROSS in names          # 始终挂载
    assert _VECTOR in names         # 默认挂载
    assert _ONLINE not in names     # 默认不挂
    assert _PERSONAL not in names   # 默认不挂


def test_vector_switch_false_removes_vector_only():
    names = _mount({"vector_search_switch": False})
    assert _CROSS in names          # cross_search 不受开关控制
    assert _VECTOR not in names


def test_online_switch_true_mounts_online():
    names = _mount({"online_search_switch": True})
    assert _ONLINE in names


def test_personal_switch_true_without_space_params_not_mounted():
    names = _mount({"personal_search_switch": True})
    assert _PERSONAL not in names   # 空间参数缺失 → 不挂


def test_personal_switch_true_with_space_params_mounted():
    names = _mount(
        {
            "personal_search_switch": True,
            "tools_param": {
                "personalKnowledgeSearch": {
                    "psnlSpaceCodeId": "PER1",
                    "psnlCategoryIdList": ["C1"],
                }
            },
        }
    )
    assert _PERSONAL in names


def test_basic_enterprise_tools_always_present():
    names = _mount({})
    # 注：query_employee_info 占位已由 contact_search 真实实现替代
    # （见 bocomadp/tools/placeholder.py 模块说明），故不再断言。
    assert {"query_internal_doc", "submit_it_ticket"} <= names


def test_tool_result_middlewares_mounted():
    mws = asyncio.run(build_enterprise_middlewares("u", "a", "s"))
    names = [type(m).__name__ for m in mws]
    assert "ToolResultPersistenceMiddleware" in names
    assert "ToolResultBudgetMiddleware" in names


def test_read_tool_result_tool_mounted():
    tools = asyncio.run(build_enterprise_tools("u", "a", "s"))
    names = [getattr(t, "name", "") for t in tools]
    assert "read_tool_result" in names  # 读回工具名固定 ASCII，不随开关切换
