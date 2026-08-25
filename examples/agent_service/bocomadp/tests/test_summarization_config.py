# -*- coding: utf-8 -*-
"""SummarizationConfig 解析与校验测试。"""
from __future__ import annotations

import pytest

from bocomadp.config.app_config import AppConfig, SummarizationConfig


class TestSummarizationConfig:
    def test_default_disabled(self) -> None:
        cfg = SummarizationConfig()
        assert cfg.enabled is False
        assert cfg.user_id is None
        assert cfg.credential_id is None
        assert cfg.model_name is None

    def test_enabled_with_fields_ok(self) -> None:
        cfg = SummarizationConfig(
            enabled=True,
            user_id="lwh",
            credential_id="deerflow-lwh-deepseek-v4-flash",
            model_name="deepseek-v4-flash",
        )
        assert cfg.credential_id == "deerflow-lwh-deepseek-v4-flash"
        assert cfg.user_id == "lwh"

    def test_enabled_missing_fields_raises(self) -> None:
        with pytest.raises(ValueError):
            SummarizationConfig(enabled=True)

    def test_enabled_missing_user_id_raises(self) -> None:
        with pytest.raises(ValueError):
            SummarizationConfig(
                enabled=True,
                credential_id="deerflow-lwh-deepseek-v4-flash",
                model_name="deepseek-v4-flash",
            )

    def test_enabled_missing_model_name_raises(self) -> None:
        with pytest.raises(ValueError):
            SummarizationConfig(
                enabled=True,
                user_id="lwh",
                credential_id="deerflow-lwh-deepseek-v4-flash",
            )

    def test_disabled_allows_missing_fields(self) -> None:
        SummarizationConfig(enabled=False)

    def test_app_config_nested_field(self) -> None:
        app = AppConfig(
            summarization=SummarizationConfig(
                enabled=True,
                user_id="lwh",
                credential_id="deerflow-lwh-deepseek-v4-flash",
                model_name="deepseek-v4-flash",
            ),
        )
        assert app.summarization.model_name == "deepseek-v4-flash"
