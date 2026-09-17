# -*- coding: utf-8 -*-
"""toolkit_whitelist 每智能体白名单过滤（反向判断：仅限制企业/MCP工具）。"""
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


def test_non_restricted_tools_always_allowed(monkeypatch):
    """非受限工具（内置/框架/项目/中间件）始终放行，不受白名单影响。"""
    monkeypatch.setattr(tw, "_restricted_tool_names", {"通讯录查询", "browser-use"})
    tk = _toolkit(["Bash", "TeamCreate", "echo", "view_image_tool", "通讯录查询"])

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
    assert "echo" in seen
    assert "view_image_tool" in seen
    assert "通讯录查询" not in seen


def test_restricted_tool_allowed_when_whitelisted(monkeypatch):
    """受限工具在白名单中才放行。"""
    monkeypatch.setattr(tw, "_restricted_tool_names", {"通讯录查询", "browser-use"})
    tk = _toolkit(["Bash", "echo", "通讯录查询"])

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
    assert "echo" in seen
    assert "通讯录查询" in seen


def test_restricted_tool_blocked_when_not_whitelisted(monkeypatch):
    """受限工具不在白名单中被过滤。"""
    monkeypatch.setattr(tw, "_restricted_tool_names", {"通讯录查询"})
    tk = _toolkit(["Bash", "通讯录查询"])

    async def fake_orig(*args, **kwargs):
        return tk

    monkeypatch.setattr(tw, "_original_get_toolkit", fake_orig)
    monkeypatch.setattr(
        "bocomadp.routers.agent_tools._tool_whitelists",
        {"ag1": []},
    )
    out = _run(tw._whitelisted_get_toolkit(agent_record=type("A", (), {"id": "ag1"})()))
    seen = _seen_names(out)
    assert "Bash" in seen
    assert "通讯录查询" not in seen
