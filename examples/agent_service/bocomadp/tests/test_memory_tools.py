# -*- coding: utf-8 -*-
"""记忆工具（custom/memory_tools）单元测试。

覆盖：消息窗口分轮组装、首轮无 assistant 回退、空消息过滤、
token 预算、两个工具的请求构造与降级错误。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import bocomadp.tools.custom.memory_tools as mt


def _run(coro):
    return asyncio.run(coro)


class _Msg:
    """最小 Msg 替身：id / role / 文本。"""

    def __init__(self, msg_id: str, role: str, text: str) -> None:
        self.id = msg_id
        self.role = role
        self._text = text

    def get_text_content(self) -> str:
        return self._text


def _u(n: int, text: str = "") -> _Msg:
    return _Msg(f"u{n}", "user", text or f"U{n}")


def _a(n: int, text: str = "") -> _Msg:
    return _Msg(f"a{n}", "assistant", text or f"A{n}")


class _Storage:
    """假框架 storage：list_messages 游标分页 + get_session 反查 agent。"""

    def __init__(self, messages: list[_Msg], agent_id: str = "agent-1") -> None:
        self.messages = messages
        self.agent_id = agent_id

    async def list_messages(
        self,
        user_id,
        session_id,
        limit=50,
        before=None,
    ):
        rows = self.messages
        if before is not None:
            index = next(
                (i for i, m in enumerate(rows) if m.id == before),
                None,
            )
            rows = rows[:index] if index is not None else []
        return rows[-limit:], len(rows) > limit

    async def get_session(self, user_id, agent_id, session_id):
        return SimpleNamespace(agent_id=self.agent_id)


def _memory_cfg(**overrides):
    base = dict(
        mcp_search_url="http://mem/search",
        mcp_save_url="http://mem/save",
        mcp_agent_key="key-1",
        mcp_search_tool="EUVDsearchMemory",
        mcp_save_tool="EUVDsaveMemoriesStandard",
        mcp_protocol_version="2025-03-26",
        mcp_timeout_seconds=30.0,
        search_default_top_k=5,
        search_max_top_k=20,
        extract_default_rounds=5,
        extract_max_rounds=20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_app(monkeypatch, cfg=None):
    cfg = cfg or _memory_cfg()
    monkeypatch.setattr(
        mt,
        "get_app_config",
        lambda: SimpleNamespace(memory=cfg),
    )
    return cfg


# ---------------------------------------------------------------------------
# 窗口组装
# ---------------------------------------------------------------------------
def test_payload_groups_multi_user_turn():
    """多条 user 才有一条 assistant 时，归为同一个对话。"""
    msgs = [_u(1), _a(1), _u(2), _a(2), _u(3), _u(4), _a(3), _u(5)]
    payload, run_id = mt.build_extract_payload(msgs, rounds=2)
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
    """首次对话：无完整轮时直接提交本轮 user，runId 回退该 user。"""
    payload, run_id = mt.build_extract_payload([_u(1)], rounds=3)
    assert payload == [{"role": "user", "content": "U1"}]
    assert run_id == "u1"


def test_payload_without_tail_user():
    """事件续跑（无新 user）：只提交 x 轮。"""
    msgs = [_u(1), _a(1), _u(2), _a(2)]
    payload, run_id = mt.build_extract_payload(msgs, rounds=2)
    assert [item["role"] for item in payload] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert run_id == "a2"


def test_payload_filters_empty_and_non_chat_roles():
    """空消息与 tool/system 角色被过滤。"""
    msgs = [
        _u(1),
        _a(1),
        _Msg("t1", "tool", "tool-result"),
        _Msg("s1", "system", "sys"),
        _a(99, "   "),
        _u(2),
    ]
    payload, run_id = mt.build_extract_payload(msgs, rounds=1)
    assert payload == [
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]
    assert run_id == "a1"


def test_payload_rounds_truncates_when_fewer_turns():
    """轮数不足时全部返回，不报错。"""
    payload, _ = mt.build_extract_payload([_u(1), _a(1)], rounds=10)
    assert len(payload) == 2


def test_payload_empty_messages():
    """无任何有效消息 → 空 payload 与空 runId。"""
    payload, run_id = mt.build_extract_payload([], rounds=3)
    assert payload == []
    assert run_id == ""


# ---------------------------------------------------------------------------
# 记忆检索工具
# ---------------------------------------------------------------------------
def _set_ctx(user="u1", session="s1", token="tok"):
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_token,
        _current_user_id,
    )

    tokens = [
        _current_user_id.set(user),
        _current_session_id.set(session),
        _current_token.set(token),
    ]
    return tokens


def _reset_ctx(tokens):
    from bocomadp.tools.agent_factory_tools import (
        _current_session_id,
        _current_token,
        _current_user_id,
    )

    _current_user_id.reset(tokens[0])
    _current_session_id.reset(tokens[1])
    _current_token.reset(tokens[2])


def test_search_rejects_empty_keyword(monkeypatch):
    _patch_app(monkeypatch)
    assert "非空" in _run(mt.memory_search("  "))


def test_search_requires_config(monkeypatch):
    _patch_app(monkeypatch, _memory_cfg(mcp_search_url=""))
    assert "未配置" in _run(mt.memory_search("咖啡"))


def test_search_returns_memory_score_and_created_at(monkeypatch):
    _patch_app(monkeypatch)
    monkeypatch.setattr(mt, "_storage", _Storage([]))
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "result": [
                {
                    "memory": "用户乳糖不耐受",
                    "score": 0.88,
                    "createdAt": "2026-11-04T10:47:12+08:00",
                },
                {"memory": "", "score": 0.1},
            ],
        }

    monkeypatch.setattr(mt, "call_memory_tool", fake_call)
    tokens = _set_ctx()
    try:
        out = _run(mt.memory_search("咖啡", topK=100))
    finally:
        _reset_ctx(tokens)

    data = json.loads(out)
    assert data["count"] == 1
    assert data["memories"][0]["createdAt"] == "2026-11-04T10:47:12+08:00"
    assert data["memories"][0]["score"] == 0.88
    # topK 超上限被截断
    assert captured["arguments"]["param"]["topK"] == 20
    assert captured["headers"]["agentId"] == "agent-1"
    assert captured["headers"]["guwp-token"] == "tok"
    assert captured["headers"]["agentPlat"] == "0"


def test_search_reports_missing_token(monkeypatch):
    _patch_app(monkeypatch)
    monkeypatch.setattr(mt, "_storage", _Storage([]))
    tokens = _set_ctx(token="")
    try:
        out = _run(mt.memory_search("咖啡"))
    finally:
        _reset_ctx(tokens)
    assert "guwpToken" in out


# ---------------------------------------------------------------------------
# 记忆提取工具
# ---------------------------------------------------------------------------
def test_extract_returns_no_message_on_empty_session(monkeypatch):
    _patch_app(monkeypatch)
    monkeypatch.setattr(mt, "_storage", _Storage([]))
    tokens = _set_ctx()
    try:
        out = _run(mt.memory_extract(rounds=3))
    finally:
        _reset_ctx(tokens)
    assert "暂无可提取" in out


def test_extract_sends_window_and_run_id(monkeypatch):
    _patch_app(monkeypatch)
    msgs = [_u(1), _a(1), _u(2), _a(2), _u(3)]
    monkeypatch.setattr(mt, "_storage", _Storage(msgs))

    async def fake_global():
        return SimpleNamespace(max_tokens=90000)

    monkeypatch.setattr(mt, "get_memory_global_config", fake_global)
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return {"result": "success"}

    monkeypatch.setattr(mt, "call_memory_tool", fake_call)
    tokens = _set_ctx()
    try:
        out = _run(mt.memory_extract(rounds=1))
    finally:
        _reset_ctx(tokens)

    data = json.loads(out)
    assert data["ok"] is True
    assert data["rounds"] == 1
    param = captured["arguments"]["param"]
    assert param["memoryType"] == 1
    assert param["inferFlag"] == 1
    assert param["messages"] == [
        {"role": "user", "content": "U2"},
        {"role": "assistant", "content": "A2"},
        {"role": "user", "content": "U3"},
    ]
    assert captured["headers"]["runId"] == "a2"
