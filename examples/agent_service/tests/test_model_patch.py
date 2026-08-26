# -*- coding: utf-8 -*-
"""model_patch：请求级 thinking/effort 合并进模型 Parameters。"""
import asyncio

from bocomadp.deerflow import model_patch as mp
from bocomadp.deerflow import run_context as rc


class _FakeModel:
    def __init__(self):
        class P:
            enable_thinking = None
            reasoning_effort = None

        self.parameters = P()


def _run(coro):
    return asyncio.run(coro)


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
