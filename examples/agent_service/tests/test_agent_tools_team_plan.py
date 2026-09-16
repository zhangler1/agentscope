# -*- coding: utf-8 -*-
"""/agents/{id}/tools 白名单接口：默认工具不可配置，企业/MCP按需添加。"""
import asyncio

from bocomadp.routers import agent_tools as at


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeRegistry:
    def list_tool_names(self):
        return ["echo", "date"]

    def list_tools(self):
        return [_FakeTool("echo"), _FakeTool("date")]


class _FakeMcpRegistry:
    def list_mcps(self):
        return []


class _FakeRequest:
    def __init__(self):
        self.app = type(
            "S",
            (),
            {
                "state": type(
                    "St",
                    (),
                    {
                        "tool_registry": _FakeRegistry(),
                        "mcp_registry": _FakeMcpRegistry(),
                    },
                )()
            },
        )()


def test_configurable_tool_names_excludes_defaults():
    """_configurable_tool_names 不含 builtins / framework / project 工具。"""
    names = at._configurable_tool_names(_FakeRequest())
    assert "Bash" not in names
    assert "TeamCreate" not in names
    assert "echo" not in names


def test_list_tools_shows_project_always_and_enterprise_only_when_whitelisted(monkeypatch):
    """GET /agents/{id}/tools: 项目工具始终显示(toggleable=False)，企业工具仅白名单中显示。"""
    monkeypatch.setattr(at, "_tool_whitelists", {"ag1": ["通讯录查询"]})

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": agent_id})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)

    resp = asyncio.run(at.list_agent_tools("ag1", _FakeRequest(), "u1"))
    tools = {t["name"]: t for t in resp["tools"]}

    assert "echo" in tools
    assert tools["echo"]["enabled"] is True
    assert tools["echo"]["toggleable"] is False

    assert "TeamCreate" not in tools
    assert "Bash" not in tools


def test_add_enterprise_tool_to_whitelist(monkeypatch):
    """PUT /agents/{id}/tools/{name} 将企业工具加入白名单。"""
    monkeypatch.setattr(at, "_tool_whitelists", {})
    store: dict = {}

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": "ag1"})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)
    monkeypatch.setattr(at, "_set_enabled_tools", lambda aid, tools: store.update({aid: tools}))
    monkeypatch.setattr(at, "_persist_whitelists", lambda: None)

    asyncio.run(at.enable_agent_tool("ag1", "通讯录查询", _FakeRequest(), "u1"))
    assert "通讯录查询" in store["ag1"]


def test_remove_tool_from_whitelist(monkeypatch):
    """DELETE /agents/{id}/tools/{name} 将工具从白名单移除。"""
    monkeypatch.setattr(at, "_tool_whitelists", {"ag1": ["通讯录查询"]})
    store: dict = {}

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": "ag1"})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)
    monkeypatch.setattr(at, "_set_enabled_tools", lambda aid, tools: store.update({aid: tools}))
    monkeypatch.setattr(at, "_persist_whitelists", lambda: None)

    asyncio.run(at.disable_agent_tool("ag1", "通讯录查询", _FakeRequest(), "u1"))
    assert "通讯录查询" not in store["ag1"]


def test_empty_whitelist_means_no_enterprise_or_mcp(monkeypatch):
    """白名单为空时，GET 不显示任何企业工具/MCP。"""
    monkeypatch.setattr(at, "_tool_whitelists", {})

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": "ag1"})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)

    resp = asyncio.run(at.list_agent_tools("ag1", _FakeRequest(), "u1"))
    tools = {t["name"]: t for t in resp["tools"]}

    for t in resp["tools"]:
        assert t["toggleable"] is False
    assert "通讯录查询" not in tools
