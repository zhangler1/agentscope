# -*- coding: utf-8 -*-
"""EllmChatModel thinking/effort 参数 → payload。"""
import asyncio

from bocomadp.credential import ELLMCredential
from bocomadp.providers.ellm_chat_model import EllmChatModel


def _model(**params) -> EllmChatModel:
    cred = ELLMCredential(
        api_key="test",
        base_url="http://localhost",
        model="Qwen3-235B-A22B",
    )
    return EllmChatModel(
        credential=cred,
        model="Qwen3-235B-A22B",
        parameters=EllmChatModel.Parameters(**params),
        stream=False,
    )


def _patch_call(monkeypatch) -> dict:
    """Patch 请求/解析两处，捕获 kwargs；stream=False 时 _call_api 还会
    调 _parse_completion_response，需一并 patch。"""
    captured: dict = {}

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_request(self, kwargs):
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(
        EllmChatModel,
        "_request_with_retry_on_auth",
        fake_request,
    )
    monkeypatch.setattr(
        EllmChatModel,
        "_parse_completion_response",
        lambda self, sdt, resp: None,
    )
    return captured


def _run(coro):
    return asyncio.run(coro)


def test_enable_thinking_payload(monkeypatch):
    m = _model(enable_thinking=True)
    captured = _patch_call(monkeypatch)
    _run(m._call_api("Qwen3-235B-A22B", []))
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_disable_thinking_payload(monkeypatch):
    m = _model(enable_thinking=False)
    captured = _patch_call(monkeypatch)
    _run(m._call_api("Qwen3-235B-A22B", []))
    assert captured["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_reasoning_effort_payload(monkeypatch):
    m = _model(reasoning_effort="high")
    captured = _patch_call(monkeypatch)
    _run(m._call_api("Qwen3-235B-A22B", []))
    assert captured["reasoning_effort"] == "high"


def test_no_params_no_extra_keys(monkeypatch):
    m = _model()
    captured = _patch_call(monkeypatch)
    _run(m._call_api("Qwen3-235B-A22B", []))
    assert "extra_body" not in captured or "chat_template_kwargs" not in captured.get("extra_body", {})
    assert "reasoning_effort" not in captured
