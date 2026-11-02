# -*- coding: utf-8 -*-
"""model_patch：请求级 thinking/effort 合并进模型 Parameters、
会话级 ``<think>`` 注入判定，以及 context_size 的数据库直读覆盖。"""
import asyncio
from types import SimpleNamespace

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
        # EllmChatModel 的三态字段：None = 未决
        self.inject_think_tag = None


def _config(**parameters):
    """会话 chat_model_config 的最小替身（只用到 ``parameters``）。"""
    return SimpleNamespace(parameters=parameters)


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


# ---------------------------------------------------------------------------
# 会话级 <think> 注入判定（方案 C：注入跟随思考模式）
# ---------------------------------------------------------------------------


def _orig_returning(model_factory):
    """每次返回一个新实例，避免同一模型被多次判定复用。"""

    async def fake_orig(user_id, config, access):
        return model_factory()

    return fake_orig


def _meta_with_tag(think_tag: bool):
    async def fake_meta(model_name):
        return {"context_size": 1_000_000, "think_tag": think_tag}

    return fake_meta


def test_session_add_think_enables_without_run_context(monkeypatch):
    """会话 parameters.add_think 是显式覆盖，原生 /chat/ 路径也生效。"""
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    model = _run(mp._patched_get_model("u1", _config(add_think=True), object()))

    assert model.inject_think_tag is True


def test_session_add_think_strict_string(monkeypatch):
    """字符串 "false" 不能被当成 True。"""
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    model = _run(mp._patched_get_model("u1", _config(add_think="false"), object()))

    assert model.inject_think_tag is False


def test_derived_needs_model_tag_and_thinking(monkeypatch):
    """模型库要 tag + 会话开思考 → 注入；关思考 → 不注入。"""
    monkeypatch.setattr(mp, "resolve_model_meta", _meta_with_tag(True))
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    on = _run(
        mp._patched_get_model("u1", _config(enable_thinking=True), object()),
    )
    off = _run(
        mp._patched_get_model("u1", _config(enable_thinking=False), object()),
    )

    assert on.inject_think_tag is True
    assert off.inject_think_tag is False


def test_derived_off_when_model_has_no_tag(monkeypatch):
    """模型库未标记 think_tag（思考走 reasoning_content）→ 开思考也不注入。"""
    monkeypatch.setattr(mp, "resolve_model_meta", _meta_with_tag(False))
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    model = _run(
        mp._patched_get_model("u1", _config(enable_thinking=True), object()),
    )

    assert model.inject_think_tag is False


def test_undecided_without_thinking_info(monkeypatch):
    """会话没给思考开关 → 保持 None，交回中间件按模型库（存量行为）。"""
    monkeypatch.setattr(mp, "resolve_model_meta", _meta_with_tag(True))
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    model = _run(mp._patched_get_model("u1", _config(), object()))

    assert model.inject_think_tag is None


def test_run_context_thinking_overrides_session_baseline(monkeypatch):
    """请求级 thinking_enabled 覆盖会话 enable_thinking，注入同步跟随。"""
    monkeypatch.setattr(mp, "resolve_model_meta", _meta_with_tag(True))
    monkeypatch.setattr(mp, "_original_get_model", _orig_returning(_FakeModel))

    token = rc.set_run_context({"thinking_enabled": False})
    try:
        model = _run(
            mp._patched_get_model("u1", _config(enable_thinking=True), object()),
        )
    finally:
        rc.reset_run_context(token)

    assert model.parameters.enable_thinking is False
    assert model.inject_think_tag is False


def test_non_ellm_model_gets_no_think_attribute(monkeypatch):
    """非 ELLM 模型没有该属性，不能被写入。"""

    class _OtherModel:
        def __init__(self):
            class P:
                enable_thinking = None
                reasoning_effort = None

            self.parameters = P()
            self.model = "deepseek-chat"
            self.context_size = 65536

    monkeypatch.setattr(
        mp,
        "_original_get_model",
        _orig_returning(_OtherModel),
    )

    model = _run(mp._patched_get_model("u1", _config(add_think=True), object()))

    assert not hasattr(model, "inject_think_tag")
