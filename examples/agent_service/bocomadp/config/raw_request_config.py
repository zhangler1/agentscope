# -*- coding: utf-8 -*-
"""外数查（raw_request）工具配置模块。

从 ``config.yaml`` 的 ``raw_request`` 节点（扁平结构）构建，仿照
``vector_search_config.py`` 的模式。仅外置设计需要的 2 项可配
（api_url / timeout）；9 个接口的路径表保留在
``bocomadp/tools/raw_request.py`` 的 ``DEFAULT_API_PATHS`` 中——
路径属接口契约几乎不变，只有域名需要跨环境（测试/生产）切换。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from .base import expand_env_vars, float_or, yaml_section

# 默认测试环境联机根地址（与 deerflow 原工程一致）
DEFAULT_API_URL = "http://12.244.66.225"


@dataclass
class RawRequestConfig:
    """外数查配置（api_url / timeout）。"""

    api_url: str = DEFAULT_API_URL
    timeout: float = 30.0

    @classmethod
    def from_yaml(cls) -> "RawRequestConfig":
        data = _base.load_config_yaml()
        rr = expand_env_vars(yaml_section(data, ["raw_request"]))
        return cls(
            api_url=str(rr.get("api_url") or DEFAULT_API_URL),
            timeout=float_or(
                None,
                rr.get("timeout") if rr.get("timeout") is not None else 30.0,
            ),
        )


def get_raw_request_config() -> RawRequestConfig:
    """返回外数查工具配置（每次读取最新 YAML / 环境值）。"""
    return RawRequestConfig.from_yaml()


__all__ = [
    "DEFAULT_API_URL",
    "RawRequestConfig",
    "get_raw_request_config",
]
