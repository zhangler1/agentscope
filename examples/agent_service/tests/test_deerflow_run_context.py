# -*- coding: utf-8 -*-
"""run_context 模块：ContextVar 通道 + 5 键提取 + 持久化委托。"""
import asyncio

import pytest

from bocomadp.deerflow import run_context as rc


def test_extract_only_known_keys():
    ctx = {
        "mode": "low",
        "reasoning_effort": "high",
        "thinking_enabled": True,
        "is_plan_mode": False,
        "subagent_enabled": True,
        "thread_id": "ignored",
        "configurable": {"x": 1},
    }
    assert rc.extract_run_context(ctx) == {
        "mode": "low",
        "reasoning_effort": "high",
        "thinking_enabled": True,
        "is_plan_mode": False,
        "subagent_enabled": True,
    }


def test_extract_none_and_empty():
    assert rc.extract_run_context(None) == {}
    assert rc.extract_run_context({}) == {}
    assert rc.extract_run_context({"foo": 1}) == {}


def test_contextvar_default_empty():
    assert rc.get_run_context() == {}


def test_set_reset_roundtrip():
    token = rc.set_run_context({"subagent_enabled": False})
    try:
        assert rc.get_run_context() == {"subagent_enabled": False}
    finally:
        rc.reset_run_context(token)
    assert rc.get_run_context() == {}


def test_save_load_delegates(monkeypatch):
    calls = {}

    async def fake_save(session_id, run_context):
        calls["saved"] = (session_id, run_context)

    async def fake_load(session_id):
        return {"mode": "x"}

    monkeypatch.setattr(rc, "save_session", fake_save)
    monkeypatch.setattr(rc, "load_run_context_from_store", fake_load)

    asyncio.run(rc.save_run_context("s1", {"mode": "x"}))
    assert calls["saved"] == ("s1", {"mode": "x"})
    assert asyncio.run(rc.load_run_context("s1")) == {"mode": "x"}
