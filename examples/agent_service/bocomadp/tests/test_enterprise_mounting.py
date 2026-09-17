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
from bocomadp.tools.enterprise import usable_tool_names

# 工具名默认中文，设置 BOCOMADP_TOOL_ASCII_NAMES=1 后为 ASCII；
# 断言用 tool_name(...) 计算期望值，避免与开关耦合。
_CROSS = tool_name("混合搜索", "cross_search")
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


#: 企业工具全集名（当前运行时形态），usableTools 全量名单。
_ALL_ENTERPRISE_NAMES: list[str] = [
    tool_name("通讯录查询", "contact_search"),
    tool_name("物理系统负责人查询", "physical_contact_search"),
    "query_internal_doc",
    "submit_it_ticket",
    tool_name("外数查", "raw_request_tool"),
    "read_tool_result",
    tool_name("汇率查询", "exchange_rate"),
    tool_name("利率查询", "interest_rate"),
    tool_name("跨知识搜索", "cross_search"),
    tool_name("行内搜索", "vector_search"),
    tool_name("个人知识库搜索", "personal_search"),
    tool_name("联网搜索", "online_search"),
]


def _mount_usable(params: dict | None) -> set[str]:
    """开关行为测试用：默认附带 usableTools 全量名单（名单模式）。"""
    merged = dict(params or {})
    merged.setdefault("usableTools", _ALL_ENTERPRISE_NAMES)
    return _mount(merged)


def test_default_mounts_cross_and_vector():
    names = _mount_usable({})
    assert _CROSS in names          # 始终挂载
    assert _VECTOR in names         # 默认挂载
    assert _ONLINE not in names     # 默认不挂
    assert _PERSONAL not in names   # 默认不挂


def test_vector_switch_false_removes_vector_only():
    names = _mount_usable({"vector_search_switch": False})
    assert _CROSS in names          # cross_search 不受开关控制
    assert _VECTOR not in names


def test_online_switch_true_mounts_online():
    names = _mount_usable({"online_search_switch": True})
    assert _ONLINE in names


def test_personal_switch_true_without_space_params_not_mounted():
    names = _mount_usable({"personal_search_switch": True})
    assert _PERSONAL not in names   # 空间参数缺失 → 不挂


def test_personal_switch_true_with_space_params_mounted():
    names = _mount_usable(
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
    names = _mount_usable({})
    # 注：query_employee_info 占位已由 contact_search 真实实现替代
    # （见 bocomadp/tools/placeholder.py 模块说明），故不再断言。
    assert {"query_internal_doc", "submit_it_ticket"} <= names


def test_tool_result_middlewares_mounted():
    mws = asyncio.run(build_enterprise_middlewares("u", "a", "s"))
    names = [type(m).__name__ for m in mws]
    assert "ToolResultPersistenceMiddleware" in names
    assert "ToolResultBudgetMiddleware" in names


def test_read_tool_result_tool_mounted():
    names = _mount_usable({})
    assert "read_tool_result" in names  # 读回工具名固定 ASCII，不随开关切换


# ---------------------------------------------------------------------------
# usableTools 请求级名单（只作用于企业工具层，优先级高于 per-agent 白名单）
# ---------------------------------------------------------------------------


def test_usable_tools_missing_mounts_nothing():
    assert _mount({}) == set()


def test_usable_tools_none_mounts_nothing():
    assert _mount({"usableTools": None}) == set()


def test_usable_tools_empty_list_mounts_nothing():
    assert _mount({"usableTools": []}) == set()


def test_usable_tools_keeps_only_listed():
    names = _mount({"usableTools": [_VECTOR, _CROSS]})
    assert names == {_VECTOR, _CROSS}


def test_usable_tools_matches_ascii_names():
    # 名单写英文名（与运行时形态无关）也能命中
    names = _mount({"usableTools": ["vector_search", "cross_search"]})
    assert names == {_VECTOR, _CROSS}


def test_usable_tools_ignores_unknown_and_non_enterprise_names():
    names = _mount({"usableTools": [_VECTOR, "Bash", "不存在的工具"]})
    assert names == {_VECTOR}


def test_usable_tools_does_not_override_switches():
    # 名单只收缩、不扩张：online_search_switch 未开，列入名单也不挂
    names = _mount({"usableTools": [_VECTOR, _ONLINE]})
    assert names == {_VECTOR}
    assert _ONLINE not in names


# ---------------------------------------------------------------------------
# usable_tool_names —— 扩管到项目工具的名单归一（项目工具名原样 +
# 企业工具名中/英文归一；用于 build_agent_tools 对项目工具层过滤）
# ---------------------------------------------------------------------------


def test_usable_tool_names_empty_returns_empty():
    assert usable_tool_names(None) == set()
    assert usable_tool_names([]) == set()
    assert usable_tool_names("not a list") == set()
    assert usable_tool_names({"k": "v"}) == set()


def test_usable_tool_names_keeps_project_tool_names_verbatim():
    # 项目工具名原样保留（无中英文之分）
    names = usable_tool_names(["echo", "get_current_time", _VECTOR])
    assert "echo" in names
    assert "get_current_time" in names
    assert _VECTOR in names


def test_usable_tool_names_normalizes_enterprise_names():
    # 企业工具中/英文名归一为当前运行时形态
    names = usable_tool_names(["行内搜索", "vector_search"])
    assert names == {_VECTOR}


def test_usable_tool_names_keeps_builtin_and_unknown_verbatim():
    # builtins / 未知名原样保留（不归一、不报错）：按工具名原样匹配，
    # 项目工具里无同名工具则自然不命中，不影响企业工具归一结果。
    names = usable_tool_names(["Bash", "不存在的工具", _VECTOR])
    assert names == {"Bash", "不存在的工具", _VECTOR}


def test_usable_tool_names_strips_whitespace():
    names = usable_tool_names(["  echo  "])
    assert names == {"echo"}


def test_usable_tool_names_skips_blank_entries():
    names = usable_tool_names(["echo", "", "  "])
    assert names == {"echo"}
