# -*- coding: utf-8 -*-
"""ContextCompressionConfig 解析与校验测试。"""
from __future__ import annotations

import pytest

from bocomadp.config.app_config import AppConfig, ContextCompressionConfig


class TestContextCompressionConfig:
    def test_default_disabled(self) -> None:
        cfg = ContextCompressionConfig()
        assert cfg.enabled is False
        assert cfg.credential_id is None
        assert cfg.model_name is None

    def test_enabled_with_fields_ok(self) -> None:
        cfg = ContextCompressionConfig(
            enabled=True,
            credential_id="deerflow-lwh-deepseek-v4-flash",
            model_name="deepseek-v4-flash",
        )
        assert cfg.credential_id == "deerflow-lwh-deepseek-v4-flash"

    def test_enabled_missing_fields_raises(self) -> None:
        with pytest.raises(ValueError):
            ContextCompressionConfig(enabled=True)

    def test_enabled_missing_model_name_raises(self) -> None:
        with pytest.raises(ValueError):
            ContextCompressionConfig(
                enabled=True,
                credential_id="deerflow-lwh-deepseek-v4-flash",
            )

    def test_disabled_allows_missing_fields(self) -> None:
        ContextCompressionConfig(enabled=False)

    def test_app_config_nested_field(self) -> None:
        app = AppConfig(
            context_compression=ContextCompressionConfig(
                enabled=True,
                credential_id="deerflow-lwh-deepseek-v4-flash",
                model_name="deepseek-v4-flash",
            ),
        )
        assert app.context_compression.model_name == "deepseek-v4-flash"
