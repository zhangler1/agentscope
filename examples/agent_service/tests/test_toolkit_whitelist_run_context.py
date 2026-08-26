# -*- coding: utf-8 -*-
"""toolkit_whitelist 每智能体白名单过滤（无请求级收窄）。

``subagent_enabled`` / ``is_plan_mode`` 在请求级被静默接受但不使用——
toolkit 过滤仅由每智能体白名单 ``_tool_whitelists`` 驱动。
"""
import asyncio

from agentscope.app._service._toolkit import Toolkit

import bocomadp.toolkit_whitelist as tw


def _tool(name: str):
    """鸭子类型工具：过滤层只读 ``getattr(tool, 'name', '')``。"""
    return type("T", (), {"name": name})()


def _toolkit(names: list[str]) -> Toolkit:
    # 注意：Toolkit 构造不允许 tool_groups 含 "basic"（ValueError），
    # tools= 参数自动生成 basic group；过滤层遍历 toolkit.tool_groups。
    return Toolkit(tools=[_tool(n) for n in names])


def _seen_names(toolkit: Toolkit) -> set[str]:
    return {t.name for g in toolkit.tool_groups for t in g.tools}


def _run(coro):
    return asyncio.run(coro)


def test_empty_whitelist_no_filter(monkeypatch):
    tk = _toolkit(["bash", "TeamCreate", "TaskCreate"])

    async def fake_orig(*args, **kwargs):
        return tk

    monkeypatch.setattr(tw, "_original_get_toolkit", fake_orig)
    monkeypatch.setattr(
        "bocomadp.routers.agent_tools._tool_whitelists",
        {},
    )
    out = _run(tw._whitelisted_get_toolkit(agent_record=type("A", (), {"id": "ag1"})()))
    assert _seen_names(out) == {"bash", "TeamCreate", "TaskCreate"}


def test_whitelist_only_keeps_allowed(monkeypatch):
    tk = _toolkit(["bash", "read", "TeamCreate", "TaskCreate"])

    async def fake_orig(*args, **kwargs):
        return tk

    monkeypatch.setattr(tw, "_original_get_toolkit", fake_orig)
    monkeypatch.setattr(
        "bocomadp.routers.agent_tools._tool_whitelists",
        {"ag1": ["bash", "TeamCreate"]},
    )
    out = _run(tw._whitelisted_get_toolkit(agent_record=type("A", (), {"id": "ag1"})()))
    assert _seen_names(out) == {"bash", "TeamCreate"}
