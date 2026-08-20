# -*- coding: utf-8 -*-
"""enterprise.py 挂载开关测试。"""
from __future__ import annotations

import asyncio

import pytest

from bocomadp.deerflow.custom_params import (
    reset_custom_params,
    set_custom_params,
)
from bocomadp.tools.enterprise import build_enterprise_tools


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
    assert "cross_search" in names          # 始终挂载
    assert "vector_search" in names         # 默认挂载
    assert "online_search" not in names     # 默认不挂
    assert "personal_search" not in names   # 默认不挂


def test_vector_switch_false_removes_vector_only():
    names = _mount({"vector_search_switch": False})
    assert "cross_search" in names          # cross_search 不受开关控制
    assert "vector_search" not in names


def test_online_switch_true_mounts_online():
    names = _mount({"online_search_switch": True})
    assert "online_search" in names


def test_personal_switch_true_without_space_params_not_mounted():
    names = _mount({"personal_search_switch": True})
    assert "personal_search" not in names   # 空间参数缺失 → 不挂


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
    assert "personal_search" in names


def test_basic_enterprise_tools_always_present():
    names = _mount({})
    assert {"query_employee_info", "query_internal_doc", "submit_it_ticket"} <= names
