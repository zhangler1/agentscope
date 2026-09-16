# -*- coding: utf-8 -*-
"""toolkit_whitelist 每智能体白名单过滤（默认工具始终保留）。"""
import asyncio

from agentscope.app._service._toolkit import Toolkit

import bocomadp.toolkit_whitelist as tw


def _tool(name: str):
    return type("T", (), {"name": name})()


def _toolkit(names: list[str]) -> Toolkit:
    return Toolkit(tools=[_tool(n) for n in names])


def _seen_names(toolkit: Toolkit) -> set[str]:
    return {t.name for g in toolkit.tool_groups for t in g.tools}


def _run(coro):
    return asyncio.run(coro)


def test_empty_whitelist_keeps_default_tools_only(monkeypatch):
    """空白名单：默认工具(Bash/Team*/Task*)+项目工具保留，企业工具被过滤。"""
    monkeypatch.setattr(tw, "_project_tool_names", {"echo"})
    tk = _toolkit(["Bash", "TeamCreate", "TaskCreate", "通讯录查询", "echo"])

    async def fake_orig(*args, **kwargs):
        return tk

    monkeypatch.setattr(tw, "_original_get_toolkit", fake_orig)
    monkeypatch.setattr(
        "bocomadp.routers.agent_tools._tool_whitelists",
        {},
    )
    out = _run(tw._whitelisted_get_toolkit(agent_record=type("A", (), {"id": "ag1"})()))
    seen = _seen_names(out)
    assert "Bash" in seen
    assert "TeamCreate" in seen
    assert "TaskCreate" in seen
    assert "echo" in seen
    assert "通讯录查询" not in seen


def test_whitelist_keeps_default_plus_whitelisted(monkeypatch):
    """非空白名单：默认+项目工具 + 白名单中的企业工具/MCP 保留。"""
    monkeypatch.setattr(tw, "_project_tool_names", {"echo"})
    tk = _toolkit(["Bash", "TeamCreate", "通讯录查询", "echo"])

    async def fake_orig(*args, **kwargs):
        return tk

    monkeypatch.setattr(tw, "_original_get_toolkit", fake_orig)
    monkeypatch.setattr(
        "bocomadp.routers.agent_tools._tool_whitelists",
        {"ag1": ["通讯录查询"]},
    )
    out = _run(tw._whitelisted_get_toolkit(agent_record=type("A", (), {"id": "ag1"})()))
    seen = _seen_names(out)
    assert "Bash" in seen
    assert "TeamCreate" in seen
    assert "echo" in seen
    assert "通讯录查询" in seen
