# -*- coding: utf-8 -*-
"""model_patch：请求级 thinking/effort 合并进模型 Parameters，
以及 context_size 的数据库直读覆盖。"""
import asyncio

import pytest

from bocomadp.deerflow import model_patch as mp
from bocomadp.deerflow import run_context as rc


class _FakeModel:
    def __init__(self):
        class P:
            enable_thinking = None
            reasoning_effort = None

        self.parameters = P()
        self.model = "deepseek-flash"
        self.context_size = 65536


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _stub_model_meta(monkeypatch):
    """隔离 DB：默认视为"库中无此模型"，覆盖行为由专门用例自行 stub。"""

    async def _none(model_name):
        return None

    monkeypatch.setattr(mp, "resolve_model_meta", _none)


def test_patch_merges_run_context(monkeypatch):
    called = {}

    async def fake_orig(user_id, config, access):
        called["orig"] = True
        return _FakeModel()

    monkeypatch.setattr(mp, "_original_get_model", fake_orig)
    token = rc.set_run_context(
        {"thinking_enabled": False, "reasoning_effort": "high"}
    )
    try:
        model = _run(mp._patched_get_model("u1", object(), object()))
    finally:
        rc.reset_run_context(token)

    assert called["orig"] is True
    assert model.parameters.enable_thinking is False
    assert model.parameters.reasoning_effort == "high"


def test_no_run_context_untouched(monkeypatch):
    async def fake_orig(user_id, config, access):
        return _FakeModel()

    monkeypatch.setattr(mp, "_original_get_model", fake_orig)
    model = _run(mp._patched_get_model("u1", object(), object()))
    assert model.parameters.enable_thinking is None
    assert model.parameters.reasoning_effort is None


def test_patch_get_model_idempotent(monkeypatch):
    from agentscope.app._service import _chat as chat_mod

    monkeypatch.setattr(mp, "_original_get_model", None)
    mp.patch_get_model()
    orig = mp._original_get_model
    assert orig is not None
    mp.patch_get_model()
    assert mp._original_get_model is orig
    assert chat_mod.get_model is mp._patched_get_model


def test_context_size_overridden_without_run_context(monkeypatch):
    """无 run_context（现有 early return 路径）也必须被覆盖。"""
    seen = {}

    async def fake_orig(user_id, config, access):
        return _FakeModel()

    async def fake_resolve(model_name):
        seen["model_name"] = model_name
        return {"context_size": 1_000_000, "think_tag": False}

    monkeypatch.setattr(mp, "_original_get_model", fake_orig)
    monkeypatch.setattr(mp, "resolve_model_meta", fake_resolve)

    model = _run(mp._patched_get_model("u1", object(), object()))

    assert seen["model_name"] == "deepseek-flash"
    assert model.context_size == 1_000_000


def test_context_size_kept_when_meta_missing(monkeypatch):
    """DB 查无此模型 / 回退也没值 → 保持构造值，不改成 0 或更小。"""
    monkeypatch.setattr(mp, "resolve_model_meta", _missing_meta)
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning_fake)

    model = _run(mp._patched_get_model("u1", object(), object()))

    assert model.context_size == 65536


async def _missing_meta(model_name):
    return None


async def _orig_returning_fake(user_id, config, access):
    return _FakeModel()
