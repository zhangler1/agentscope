# -*- coding: utf-8 -*-
"""ImageParseConfig 解析与校验测试。"""
from __future__ import annotations

import pytest

from bocomadp.config.app_config import ImageParseConfig


class TestImageParseConfig:
    def test_default_disabled(self) -> None:
        cfg = ImageParseConfig()
        assert cfg.enabled is False
        assert cfg.user_id is None
        assert cfg.credential_id is None
        assert cfg.model_name is None

    def test_enabled_with_fields_ok(self) -> None:
        cfg = ImageParseConfig(
            enabled=True,
            user_id="lwh",
            credential_id="bocom_ellm-lwh-qwen3-vl",
            model_name="Qwen3-VL-30B-A3B-Instruct",
        )
        assert cfg.credential_id == "bocom_ellm-lwh-qwen3-vl"
        assert cfg.user_id == "lwh"

    def test_enabled_missing_fields_raises(self) -> None:
        with pytest.raises(ValueError):
            ImageParseConfig(enabled=True)

    def test_enabled_missing_user_id_raises(self) -> None:
        with pytest.raises(ValueError):
            ImageParseConfig(
                enabled=True,
                credential_id="bocom_ellm-lwh-qwen3-vl",
                model_name="Qwen3-VL-30B-A3B-Instruct",
            )

    def test_enabled_missing_model_name_raises(self) -> None:
        with pytest.raises(ValueError):
            ImageParseConfig(
                enabled=True,
                user_id="lwh",
                credential_id="bocom_ellm-lwh-qwen3-vl",
            )

    def test_disabled_allows_missing_fields(self) -> None:
        ImageParseConfig(enabled=False)
