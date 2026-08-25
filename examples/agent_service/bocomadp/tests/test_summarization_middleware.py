# -*- coding: utf-8 -*-
"""SummarizationMiddleware 测试（on_compress_context 钩子）。"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from bocomadp.config.app_config import SummarizationConfig
from bocomadp.credential import ELLMCredential
from bocomadp.middleware.summarization import SummarizationMiddleware
from bocomadp.providers.ellm_key import EllmKeyRefresher

_CREDENTIAL = ELLMCredential(
    api_key="test-key",
    base_url="http://localhost",
    model=None,
    scene_code="P2024146",
    api_key_url="http://localhost/createSceneApiKey.do",
)


class _FakeStorage:
    """Minimal StorageBase stand-in: get_credential returns a record."""

    def __init__(self, record_data: dict | None = None) -> None:
        self._record = (
            MagicMock(data=record_data) if record_data is not None else None
        )
        self.get_credential = AsyncMock(return_value=self._record)


def _make_cfg(enabled: bool = True) -> SummarizationConfig:
    return SummarizationConfig(
        enabled=enabled,
        user_id="lwh",
        credential_id="deerflow-lwh-deepseek-v4-flash",
        model_name="deepseek-v4-flash",
    )


def _make_agent() -> MagicMock:
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.context_size = 128_000
    return agent


def _patch_get_config(cfg: SummarizationConfig | None):
    """patch 中间件内 get_typed_config 返回指定配置。"""
    return mock.patch(
        "bocomadp.middleware.summarization.get_typed_config",
        new=AsyncMock(return_value=cfg),
    )


class TestSummarizationMiddleware(IsolatedAsyncioTestCase):
    async def test_no_config_passthrough(self) -> None:
        storage = _FakeStorage(_CREDENTIAL.model_dump())
        mw = SummarizationMiddleware(storage, MagicMock())
        agent = _make_agent()
        next_handler = AsyncMock(return_value=None)

        with _patch_get_config(None):  # DB 无记录 → 未启用
            await mw.on_compress_context(agent, {}, next_handler)

        next_handler.assert_awaited_once()
        storage.get_credential.assert_not_awaited()

    async def test_disabled_passthrough(self) -> None:
        storage = _FakeStorage(_CREDENTIAL.model_dump())
        mw = SummarizationMiddleware(storage, MagicMock())
        agent = _make_agent()
        next_handler = AsyncMock(return_value=None)

        with _patch_get_config(_make_cfg(enabled=False)):
            await mw.on_compress_context(agent, {}, next_handler)

        next_handler.assert_awaited_once()
        storage.get_credential.assert_not_awaited()

    async def test_credential_missing_falls_back_to_session_model(self) -> None:
        storage = _FakeStorage(None)  # 查不到凭证
        mw = SummarizationMiddleware(storage, MagicMock())
        agent = _make_agent()
        next_handler = AsyncMock(return_value=None)

        with _patch_get_config(_make_cfg(enabled=True)):
            await mw.on_compress_context(agent, {}, next_handler)

        next_handler.assert_awaited_once()  # 透传，未 swap

    async def test_success_swaps_and_restores(self) -> None:
        storage = _FakeStorage(_CREDENTIAL.model_dump())
        mw = SummarizationMiddleware(storage, MagicMock())
        agent = _make_agent()
        original_model = agent.model
        next_handler = AsyncMock(return_value=None)

        with _patch_get_config(_make_cfg(enabled=True)):
            with mock.patch.object(
                EllmKeyRefresher,
                "ensure_fresh_key",
                new=AsyncMock(return_value=("refreshed-key", storage._record)),
            ) as ensure:
                await mw.on_compress_context(agent, {}, next_handler)

        ensure.assert_awaited_once_with("deerflow-lwh-deepseek-v4-flash")
        next_handler.assert_awaited_once()
        assert agent.model is original_model  # finally 恢复
        # 压缩临时模型注入刷新后的 key
        storage.get_credential.assert_awaited_once_with(
            "lwh",
            "deerflow-lwh-deepseek-v4-flash",
        )

    async def test_failure_retries_with_session_model(self) -> None:
        storage = _FakeStorage(_CREDENTIAL.model_dump())
        mw = SummarizationMiddleware(storage, MagicMock())
        agent = _make_agent()
        original_model = agent.model
        calls = {"n": 0}

        async def failing_then_ok() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("compression model boom")
            return None

        next_handler = AsyncMock(side_effect=failing_then_ok)

        with _patch_get_config(_make_cfg(enabled=True)):
            with mock.patch.object(
                EllmKeyRefresher,
                "ensure_fresh_key",
                new=AsyncMock(return_value=("refreshed-key", storage._record)),
            ):
                await mw.on_compress_context(agent, {}, next_handler)

        assert calls["n"] == 2  # 第一次（统一模型）失败，第二次（会话模型）成功
        assert agent.model is original_model
