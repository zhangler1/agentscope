# -*- coding: utf-8 -*-
"""raw_request 配置模块测试（monkeypatch config.yaml 内容，不发真实网络）。"""
from __future__ import annotations

import pytest

from bocomadp.config import base as _base
from bocomadp.config.raw_request_config import (
    DEFAULT_API_URL,
    RawRequestConfig,
    get_raw_request_config,
)


@pytest.fixture(autouse=True)
def _fake_yaml(monkeypatch):
    fake = {
        "raw_request": {
            "api_url": "$RAW_REQUEST_API_URL",
            "timeout": 15,
        },
    }
    monkeypatch.setattr(_base, "load_config_yaml", lambda: dict(fake))


def test_raw_request_config():
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("RAW_REQUEST_API_URL", "http://gw/raw.do")
    try:
        cfg = get_raw_request_config()
        assert isinstance(cfg, RawRequestConfig)
        assert cfg.api_url == "http://gw/raw.do"   # $VAR 展开生效
        assert cfg.timeout == 15
    finally:
        monkeypatch_env.undo()


def test_raw_request_config_defaults(monkeypatch):
    # 节点缺失 → 全部回退默认值（域名 + 超时 30s）
    monkeypatch.setattr(_base, "load_config_yaml", lambda: {})
    cfg = get_raw_request_config()
    assert cfg.api_url == DEFAULT_API_URL
    assert cfg.timeout == 30.0


def test_raw_request_config_missing_node_fields(monkeypatch):
    # 节点存在但只给了 api_url → timeout 回退默认
    monkeypatch.setattr(
        _base,
        "load_config_yaml",
        lambda: {"raw_request": {"api_url": "http://x"}},
    )
    cfg = get_raw_request_config()
    assert cfg.api_url == "http://x"
    assert cfg.timeout == 30.0


def test_domain_default_constant():
    # 默认域名与 deerflow 原工程一致（联机根地址）
    assert DEFAULT_API_URL == "http://12.244.66.225"
