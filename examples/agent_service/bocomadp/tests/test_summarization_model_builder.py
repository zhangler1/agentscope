# -*- coding: utf-8 -*-
"""临时压缩模型构建测试。"""
from __future__ import annotations

import asyncio
from unittest import mock

from bocomadp.credential import ELLMCredential
from bocomadp.summarization_model_builder import (
    build_summarization_model,
    effective_context_size,
)

_CREDENTIAL = ELLMCredential(
    api_key="test-key",
    base_url="http://localhost",
    model=None,  # 凭证不绑定单模型（新逻辑）
    scene_code="P2024146",
    api_key_url="http://localhost/createSceneApiKey.do",
)

_PATCH_TARGET = "bocomadp.summarization_model_builder.resolve_model_meta"


class TestEffectiveContextSize:
    @mock.patch(
        _PATCH_TARGET,
        new=mock.AsyncMock(return_value={"context_size": 1_000_000}),
    )
    def test_session_smaller_wins(self) -> None:
        assert asyncio.run(effective_context_size(65_536, "m")) == 65_536

    @mock.patch(
        _PATCH_TARGET,
        new=mock.AsyncMock(return_value={"context_size": 32_768}),
    )
    def test_model_smaller_wins(self) -> None:
        assert asyncio.run(effective_context_size(128_000, "m")) == 32_768

    @mock.patch(_PATCH_TARGET, new=mock.AsyncMock(return_value=None))
    def test_fallback_when_model_unknown(self) -> None:
        """库中无此模型 → 沿用既有 65536 语义。"""
        assert (
            asyncio.run(effective_context_size(1_000_000, "missing")) == 65_536
        )


class TestBuildSummarizationModel:
    @mock.patch(
        _PATCH_TARGET,
        new=mock.AsyncMock(return_value={"context_size": 1_000_000}),
    )
    def test_builds_with_configured_model_name(self) -> None:
        model = asyncio.run(
            build_summarization_model(
                _CREDENTIAL.model_dump(),
                model_name="deepseek-v4-flash",
                session_context_size=128_000,
            ),
        )
        assert model.model == "deepseek-v4-flash"

    @mock.patch(
        _PATCH_TARGET,
        new=mock.AsyncMock(return_value={"context_size": 1_000_000}),
    )
    def test_context_size_is_min(self) -> None:
        model = asyncio.run(
            build_summarization_model(
                _CREDENTIAL.model_dump(),
                model_name="deepseek-v4-flash",
                session_context_size=65_536,
            ),
        )
        assert model.context_size == 65_536
