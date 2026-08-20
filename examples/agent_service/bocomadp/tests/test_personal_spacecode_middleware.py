# -*- coding: utf-8 -*-
"""PersonalSpacecodeOverrideMiddleware 测试：从 custom_params 强制覆盖空间参数。"""
from __future__ import annotations

import asyncio

from bocomadp.deerflow.custom_params import (
    get_custom_params,
    reset_custom_params,
    set_custom_params,
)
from bocomadp.tools.personal_search import PersonalSpacecodeOverrideMiddleware


def _collect(tool, kwargs):
    """以无操作 next_handler 运行中间件，返回最终 kwargs。"""
    async def _next(**kw):
        return kw

    async def _run():
        final_kwargs = None
        async for chunk in PersonalSpacecodeOverrideMiddleware().on_tool_call(
            tool,
            kwargs,
            _next,
        ):
            final_kwargs = chunk
        return final_kwargs

    return asyncio.run(_run())


def test_override_space_code_id_and_list():
    token = set_custom_params(
        {
            "tools_param": {
                "personalKnowledgeSearch": {
                    "psnlSpaceCodeId": "PER123",
                    "psnlCategoryIdList": ["CATE1", "CATE2"],
                }
            }
        }
    )
    try:
        result = _collect(None, {"keyword": "q", "space_code_id": "wrong"})
        assert result["space_code_id"] == "PER123"
        assert result["space_code"] == ["CATE1", "CATE2"]
    finally:
        reset_custom_params(token)


def test_no_override_when_params_missing():
    token = set_custom_params({})
    try:
        result = _collect(None, {"keyword": "q"})
        assert "space_code_id" not in result
        assert "space_code" not in result
    finally:
        reset_custom_params(token)


def test_space_code_str_normalized_to_list():
    token = set_custom_params(
        {
            "tools_param": {
                "personalKnowledgeSearch": {
                    "psnlSpaceCodeId": "P1",
                    "psnlCategoryIdList": "CATE9",
                }
            }
        }
    )
    try:
        result = _collect(None, {"keyword": "q"})
        assert result["space_code"] == ["CATE9"]   # str → list[str]
    finally:
        reset_custom_params(token)
