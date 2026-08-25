# -*- coding: utf-8 -*-
"""临时多模态模型构建测试。"""
from __future__ import annotations

from agentscope.formatter import OpenAIChatFormatter

from bocomadp.credential import ELLMCredential
from bocomadp.view_image_model_builder import build_image_parse_model

_CREDENTIAL = ELLMCredential(
    api_key="test-key",
    base_url="http://localhost",
    model=None,  # 凭证不绑定单模型（新逻辑）
    scene_code="P2024146",
    api_key_url="http://localhost/createSceneApiKey.do",
)


class TestBuildImageParseModel:
    def test_builds_with_configured_model_name(self) -> None:
        model = build_image_parse_model(
            _CREDENTIAL.model_dump(),
            model_name="Qwen3-VL-30B-A3B-Instruct",
        )
        assert model.model == "Qwen3-VL-30B-A3B-Instruct"

    def test_formatter_is_openai_compatible(self) -> None:
        """图片输入依赖 OpenAI 兼容 formatter（DeepSeekChatFormatter 不支持）。"""
        model = build_image_parse_model(
            _CREDENTIAL.model_dump(),
            model_name="Qwen3-VL-30B-A3B-Instruct",
        )
        assert isinstance(model.formatter, OpenAIChatFormatter)
