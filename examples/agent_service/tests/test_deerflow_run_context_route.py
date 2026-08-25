# -*- coding: utf-8 -*-
"""deerflow 路由层 run_context 解析与注入。"""
import asyncio

from bocomadp.deerflow.routers import deerflow_chat as chat_mod


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
