# -*- coding: utf-8 -*-
"""临时压缩模型构建测试。"""
from __future__ import annotations

from unittest import mock

import pytest

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


class TestEffectiveContextSize:
    @mock.patch(
        "bocomadp.summarization_model_builder._get_model_context_size",
        return_value=1_000_000,
    )
    def test_session_smaller_wins(self, _mock: mock.MagicMock) -> None:
        assert effective_context_size(65_536, "deepseek-v4-flash") == 65_536

    @mock.patch(
        "bocomadp.summarization_model_builder._get_model_context_size",
        return_value=32_768,
    )
    def test_model_smaller_wins(self, _mock: mock.MagicMock) -> None:
        assert effective_context_size(128_000, "deepseek-v4-flash") == 32_768


class TestBuildSummarizationModel:
    def test_builds_with_configured_model_name(self) -> None:
        model = build_summarization_model(
            _CREDENTIAL.model_dump(),
            model_name="deepseek-v4-flash",
            session_context_size=128_000,
        )
        assert model.model == "deepseek-v4-flash"

    @mock.patch(
        "bocomadp.summarization_model_builder._get_model_context_size",
        return_value=1_000_000,
    )
    def test_context_size_is_min(self, _mock: mock.MagicMock) -> None:
        model = build_summarization_model(
            _CREDENTIAL.model_dump(),
            model_name="deepseek-v4-flash",
            session_context_size=65_536,
        )
        assert model.context_size == 65_536
