# -*- coding: utf-8 -*-
"""三个搜索工具配置模块测试（monkeypatch config.yaml 内容）。"""
from __future__ import annotations

import pytest

from bocomadp.config import base as _base
from bocomadp.config.online_search_config import (
    OnlineSearchConfig,
    get_online_search_config,
)
from bocomadp.config.personal_search_config import (
    PersonalSearchConfig,
    get_personal_search_config,
)
from bocomadp.config.vector_search_config import (
    VectorSearchConfig,
    get_vector_search_config,
)


@pytest.fixture(autouse=True)
def _fake_yaml(monkeypatch):
    fake = {
        "online_search": {
            "api_url": "$CUSTOM_SEARCH_API_URL",
            "timeout": 30,
            "max_results": 5,
        },
        "personal_search": {
            "api_url": "http://personal/search",
            "timeout": 30,
            "source_type": "WDZS",
            "repository": "personal-search",
            "search_type": "0",
            "headers": {"jumpCloud-Env": "BASE"},
        },
        "vector_search": {
            "api_url": "http://vector/search",
            "timeout": 30,
            "page_size": 10,
            "text_top_n": 7,
            "vector_top_n": 10,
            "space_codes": ["SP0999999", "SP0000214"],
        },
    }
    monkeypatch.setattr(_base, "load_config_yaml", lambda: dict(fake))


def test_online_search_config():
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("CUSTOM_SEARCH_API_URL", "http://gw/querySources.do")
    try:
        cfg = get_online_search_config()
        assert isinstance(cfg, OnlineSearchConfig)
        assert cfg.api_url == "http://gw/querySources.do"   # $VAR 展开
        assert cfg.timeout == 30
        assert cfg.max_results == 5
    finally:
        monkeypatch_env.undo()


def test_online_search_config_defaults(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {"online_search": {"api_url": "http://x"}},
    )
    cfg = get_online_search_config()
    assert cfg.timeout == 30.0
    assert cfg.max_results == 5


def test_personal_search_config():
    cfg = get_personal_search_config()
    assert isinstance(cfg, PersonalSearchConfig)
    assert cfg.api_url == "http://personal/search"
    assert cfg.source_type == "WDZS"
    assert cfg.repository == "personal-search"
    assert cfg.search_type == "0"
    assert cfg.headers["jumpCloud-Env"] == "BASE"  # 配置合并到默认头


def test_personal_search_headers_merge_with_defaults():
    cfg = get_personal_search_config()
    assert "User-Agent" in cfg.headers            # 默认头保留
    assert cfg.headers["jumpCloud-Env"] == "BASE"  # 配置覆盖


def test_vector_search_config():
    cfg = get_vector_search_config()
    assert isinstance(cfg, VectorSearchConfig)
    assert cfg.api_url == "http://vector/search"
    assert cfg.page_size == 10
    assert cfg.text_top_n == 7
    assert cfg.vector_top_n == 10
    assert cfg.space_codes == ["SP0999999", "SP0000214"]


def test_vector_search_config_defaults(monkeypatch):
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {"vector_search": {"api_url": "http://y"}},
    )
    cfg = get_vector_search_config()
    assert cfg.timeout == 30.0
    assert cfg.page_size == 10
    assert cfg.text_top_n == 7
    assert cfg.vector_top_n == 10
    assert cfg.space_codes == ["SP0999999"]
