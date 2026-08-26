# -*- coding: utf-8 -*-
"""/agents/{id}/tools 白名单接口：团队/规划工具可见可控。"""
import asyncio

from bocomadp.routers import agent_tools as at

_TEAM_TOOLS = {"TeamCreate", "AgentCreate", "TeamSay", "TeamDelete", "AgentInvite"}
_PLAN_TOOLS = {"TaskCreate", "TaskList", "TaskGet", "TaskUpdate"}


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


def test_all_tool_names_includes_team_and_plan():
    names = at._all_tool_names(_FakeRequest())
    assert _TEAM_TOOLS <= names
    assert _PLAN_TOOLS <= names


def test_disable_team_tool_expands_full_list(monkeypatch):
    """DELETE AgentCreate：白名单从 [] 展开为全量-1（含团队/规划工具）。"""
    monkeypatch.setattr(at, "_tool_whitelists", {})
    store: dict = {}

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": "ag1"})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)
    monkeypatch.setattr(at, "_set_enabled_tools", lambda aid, tools: store.update({aid: tools}))
    monkeypatch.setattr(at, "_persist_whitelists", lambda: None)

    asyncio.run(at.disable_agent_tool("ag1", "AgentCreate", _FakeRequest(), "u1"))
    saved = store["ag1"]
    assert "AgentCreate" not in saved
    assert "TeamCreate" in saved and "TaskCreate" in saved


def test_list_tools_includes_team_and_plan_with_description(monkeypatch):
    """GET /agents/{id}/tools 返回团队/规划工具条目且带 description。"""
    monkeypatch.setattr(at, "_tool_whitelists", {})

    async def fake_resolve(request, user_id, agent_id):
        return type("A", (), {"id": agent_id})()

    monkeypatch.setattr(at, "_resolve_framework_agent", fake_resolve)

    resp = asyncio.run(at.list_agent_tools("ag1", _FakeRequest(), "u1"))
    tools = {t["name"]: t for t in resp["tools"]}

    meta_by_name = {m["name"]: m for m in at._FRAMEWORK_TOOLS_META}
    assert set(meta_by_name) <= set(tools)
    for name, meta in meta_by_name.items():
        assert tools[name]["description"] == meta["description"]
        assert tools[name]["description"]  # 非空
        assert tools[name]["toggleable"] is True
