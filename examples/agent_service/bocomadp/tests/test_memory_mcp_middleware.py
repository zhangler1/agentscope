# -*- coding: utf-8 -*-
"""记忆 MCP 工具中间件与展示层装饰单元测试。

覆盖：窗口分轮组装、检索参数规范化、提取参数组装（rounds → messages）、
MCPTool 装饰（schema / 中间件 / 只读）与 client 材料化。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bocomadp.memory.mcp_tool_middleware as mw
import bocomadp.toolkit_whitelist as tw


def _run(coro):
    return asyncio.run(coro)


class _Msg:
    def __init__(self, msg_id: str, role: str, text: str) -> None:
        self.id = msg_id
        self.role = role
        self._text = text

    def get_text_content(self) -> str:
        return self._text


def _u(n: int) -> _Msg:
    return _Msg(f"u{n}", "user", f"U{n}")


def _a(n: int) -> _Msg:
    return _Msg(f"a{n}", "assistant", f"A{n}")


class _Storage:
    def __init__(self, messages: list[_Msg]) -> None:
        self.messages = messages

    async def list_messages(self, user_id, session_id, limit=50, before=None):
        rows = self.messages
        if before is not None:
            index = next(
                (i for i, m in enumerate(rows) if m.id == before),
                None,
            )
            rows = rows[:index] if index is not None else []
        return rows[-limit:], len(rows) > limit


def _memory_cfg(**overrides):
    base = dict(
        mcp_search_tool="EUVDsearchMemory",
        mcp_save_tool="EUVDsaveMemoriesStandard",
        search_default_top_k=5,
        search_max_top_k=20,
        extract_default_rounds=5,
        extract_max_rounds=20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _set_ctx(user="u1", session="s1"):
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_user_id,
    )

    return [
        _current_user_id.set(user),
        _current_session_id.set(session),
    ]


def _reset_ctx(tokens):
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_user_id,
    )

    _current_user_id.reset(tokens[0])
    _current_session_id.reset(tokens[1])


# ---------------------------------------------------------------------------
# 窗口组装（迁移自旧工具测试）
# ---------------------------------------------------------------------------
def test_payload_groups_multi_user_turn():
    msgs = [_u(1), _a(1), _u(2), _a(2), _u(3), _u(4), _a(3), _u(5)]
    payload, run_id = mw.build_extract_payload(msgs, rounds=2)
    assert payload == [
        {"role": "user", "content": "U2"},
        {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "U3"},
        {"role": "user", "content": "U4"},
        {"role": "assistant", "content": "A3"},
        {"role": "user", "content": "U5"},
    ]
    assert run_id == "a3"


def test_payload_first_turn_without_assistant():
    payload, run_id = mw.build_extract_payload([_u(1)], rounds=3)
    assert payload == [{"role": "user", "content": "U1"}]
    assert run_id == "u1"


def test_payload_filters_empty_and_non_chat_roles():
    msgs = [
        _u(1),
        _a(1),
        _Msg("t1", "tool", "tool-result"),
        _Msg("s1", "system", "sys"),
        _Msg("a99", "assistant", "   "),
        _u(2),
    ]
    payload, _ = mw.build_extract_payload(msgs, rounds=1)
    assert payload == [
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]


# ---------------------------------------------------------------------------
# 检索参数
# ---------------------------------------------------------------------------
def test_search_args_default_topk():
    out = mw.normalize_search_args({"keyword": " 咖啡 "}, _memory_cfg())
    assert out == {"param": {"keyword": "咖啡", "topK": 5}}


def test_search_args_capped_by_config_and_platform():
    out = mw.normalize_search_args(
        {"keyword": "咖啡", "topK": 999},
        _memory_cfg(search_max_top_k=50),
    )
    assert out["param"]["topK"] == 20  # 平台硬上限


def test_search_args_reject_empty_keyword():
    try:
        mw.normalize_search_args({"keyword": "   "}, _memory_cfg())
    except ValueError as exc:
        assert "keyword" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_search_args_reject_negative_topk():
    try:
        mw.normalize_search_args(
            {"keyword": "x", "topK": -1},
            _memory_cfg(),
        )
    except ValueError as exc:
        assert "topK" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# 提取参数
# ---------------------------------------------------------------------------
def test_save_args_builds_messages(monkeypatch):
    monkeypatch.setattr(mw, "_storage", _Storage([_u(1), _a(1), _u(2)]))
    tokens = _set_ctx()

    async def fake_global():
        return SimpleNamespace(max_tokens=90000)

    monkeypatch.setattr(mw, "get_memory_global_config", fake_global)
    try:
        out = _run(mw.build_save_args({"rounds": 1}, _memory_cfg()))
    finally:
        _reset_ctx(tokens)

    assert out == {
        "param": {
            "memoryType": 1,
            "inferFlag": 1,
            "messages": [
                {"role": "user", "content": "U1"},
                {"role": "assistant", "content": "A1"},
                {"role": "user", "content": "U2"},
            ],
        },
    }


def test_save_args_raises_on_empty_session(monkeypatch):
    monkeypatch.setattr(mw, "_storage", _Storage([]))
    tokens = _set_ctx()
    try:
        _run(mw.build_save_args({"rounds": 3}, _memory_cfg()))
    except ValueError as exc:
        assert "暂无可提取" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    finally:
        _reset_ctx(tokens)


def test_save_args_rejects_missing_context(monkeypatch):
    monkeypatch.setattr(mw, "_storage", _Storage([_u(1)]))
    tokens = _set_ctx(user="", session="")
    try:
        _run(mw.build_save_args({"rounds": 1}, _memory_cfg()))
    except ValueError as exc:
        assert "会话上下文" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    finally:
        _reset_ctx(tokens)


# ---------------------------------------------------------------------------
# MCPTool 装饰
# ---------------------------------------------------------------------------
class _FakeMCPTool:
    def __init__(self, server_name: str) -> None:
        self._tool = SimpleNamespace(name=server_name)
        self.input_schema = {"type": "object"}
        self._middlewares: list = []
        self.is_read_only = False
        self.name = f"mcp__euvd-memory__{server_name}"


def test_decorate_search_tool():
    tool = _FakeMCPTool("EUVDsearchMemory")
    tw._decorate_memory_tool(tool, _memory_cfg())
    props = tool.input_schema["properties"]
    assert "keyword" in props and "topK" in props
    assert "param" not in props
    assert tool.input_schema["required"] == ["keyword"]
    assert tool.is_read_only is True
    assert len(tool._middlewares) == 1


def test_decorate_save_tool():
    tool = _FakeMCPTool("EUVDsaveMemoriesStandard")
    tw._decorate_memory_tool(tool, _memory_cfg())
    props = tool.input_schema["properties"]
    assert "rounds" in props
    assert props["rounds"]["maximum"] == 20
    assert tool.is_read_only is True
    assert len(tool._middlewares) == 1


def test_decorate_ignores_other_tools():
    tool = _FakeMCPTool("some-other-tool")
    before = dict(tool.input_schema)
    tw._decorate_memory_tool(tool, _memory_cfg())
    assert tool.input_schema == before
    assert tool._middlewares == []
    assert tool.is_read_only is False


def test_materialize_moves_tools_and_drops_client(monkeypatch):
    class _Client:
        name = "euvd-memory"

        async def list_tools(self):
            return [
                _FakeMCPTool("EUVDsearchMemory"),
                _FakeMCPTool("EUVDsaveMemoriesStandard"),
            ]

    group = SimpleNamespace(tools=[], mcps=[_Client()])
    toolkit = SimpleNamespace(tool_groups=[group])
    monkeypatch.setattr(
        "bocomadp.config.get_app_config",
        lambda: SimpleNamespace(memory=_memory_cfg()),
    )
    _run(tw._materialize_memory_mcp(toolkit))
    assert len(group.tools) == 2
    assert group.mcps == []


def test_materialize_uses_registry_client_when_gateway_wrapped(monkeypatch):
    """沙箱 gateway client 只带静态头；应换回注册表的原始 client。"""
    calls = {"gateway": 0, "original": 0}

    class _GatewayClient:
        name = "euvd-memory"

        async def list_tools(self):
            calls["gateway"] += 1
            return []

    class _OriginalClient:
        name = "euvd-memory"

        async def list_tools(self):
            calls["original"] += 1
            return [
                _FakeMCPTool("EUVDsearchMemory"),
                _FakeMCPTool("EUVDsaveMemoriesStandard"),
            ]

    original = _OriginalClient()
    registry = SimpleNamespace(list_mcps=lambda: [original])
    gateway = _GatewayClient()
    group = SimpleNamespace(tools=[], mcps=[gateway])
    toolkit = SimpleNamespace(tool_groups=[group])

    monkeypatch.setattr(
        "bocomadp.config.get_app_config",
        lambda: SimpleNamespace(memory=_memory_cfg()),
    )
    monkeypatch.setattr(tw, "_mcp_registry", registry)

    _run(tw._materialize_memory_mcp(toolkit))

    assert calls == {"gateway": 0, "original": 1}
    assert len(group.tools) == 2
    assert group.mcps == []


def test_materialize_keeps_client_on_list_failure(monkeypatch):
    class _Client:
        name = "euvd-memory"

        async def list_tools(self):
            raise RuntimeError("platform down")

    client = _Client()
    group = SimpleNamespace(tools=[], mcps=[client])
    toolkit = SimpleNamespace(tool_groups=[group])
    monkeypatch.setattr(
        "bocomadp.config.get_app_config",
        lambda: SimpleNamespace(memory=_memory_cfg()),
    )
    _run(tw._materialize_memory_mcp(toolkit))
    assert group.tools == []
    assert group.mcps == [client]
