# -*- coding: utf-8 -*-
"""deerflow 路由层 run_context 解析与注入。"""
import asyncio

from bocomadp.deerflow.routers import deerflow_chat as chat_mod
from bocomadp.deerflow.run_context import extract_run_context


def test_resolve_saves_when_requested(monkeypatch):
    calls = {}

    async def fake_save(session_id, params):
        calls["save"] = (session_id, params)

    async def fake_load(session_id):
        calls["load"] = True
        return None

    monkeypatch.setattr(chat_mod, "save_run_context", fake_save)
    monkeypatch.setattr(chat_mod, "load_run_context", fake_load)

    requested = {"subagent_enabled": False, "mode": "low"}
    out = asyncio.run(chat_mod._resolve_run_context("s1", requested))
    assert out == requested
    assert calls["save"] == ("s1", requested)
    assert "load" not in calls


def test_resolve_loads_when_not_requested(monkeypatch):
    calls = {}

    async def fake_load(session_id):
        calls["load"] = session_id
        return {"is_plan_mode": False}

    monkeypatch.setattr(chat_mod, "load_run_context", fake_load)
    assert asyncio.run(chat_mod._resolve_run_context("s1", None)) == {
        "is_plan_mode": False
    }
    assert calls["load"] == "s1"


def test_resolve_fail_open(monkeypatch):
    async def fake_load(session_id):
        return None

    monkeypatch.setattr(chat_mod, "load_run_context", fake_load)
    assert asyncio.run(chat_mod._resolve_run_context("s1", None)) == {}


def test_extract_from_context_filters_none():
    """context 内根路径 5 键提取：只取有值键，值为 None 的忽略。"""
    body = chat_mod.CreateRunRequest(
        session_id="s1",
        context={
            "reasoning_effort": "high",
            "thinking_enabled": True,
            "is_plan_mode": False,      # False 是有效值，应保留
            "subagent_enabled": None,   # None 应被忽略
            "mode": "low",
        },
    )
    out = extract_run_context(body.context)
    assert out == {
        "reasoning_effort": "high",
        "thinking_enabled": True,
        "is_plan_mode": False,
        "mode": "low",
    }
    assert "subagent_enabled" not in out


def test_split_request_context_partitions_channels():
    """context 按通道拆分：根路径 5 键 → run_context，其余 → custom_params。"""
    params_part, run_context_part = chat_mod._split_request_context(
        {
            "mode": "low",
            "thinking_enabled": True,
            "model_name": "deepseek-v4-flash",
            "llm_model_name": "deepseek-v4-flash",
            "space_code_list": ["SP01"],
            "files": ["f1"],
        },
    )
    assert run_context_part == {"mode": "low", "thinking_enabled": True}
    assert params_part == {
        "model_name": "deepseek-v4-flash",
        "llm_model_name": "deepseek-v4-flash",
        "space_code_list": ["SP01"],
        "files": ["f1"],
    }


def test_split_request_context_empty_params_returns_none():
    """context 仅含根路径 5 键 / 缺失时 custom_params 部分为 None（触发回退）。"""
    params_part, run_context_part = chat_mod._split_request_context(
        {"mode": "low"},
    )
    assert params_part is None
    assert run_context_part == {"mode": "low"}
    assert chat_mod._split_request_context(None) == (None, {})
