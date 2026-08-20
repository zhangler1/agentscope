# -*- coding: utf-8 -*-
"""联网搜索工具（online_search）配置模块。

从 ``config.yaml`` 的 ``online_search`` 节点（扁平结构）构建，字段名与
YAML 键一一对应；``$VAR`` / ``${VAR}`` 环境变量引用经
:func:`base.expand_env_vars` 展开。实现对齐 ``cross_search_config.py``。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from .base import expand_env_vars, float_or, int_or, yaml_section


@dataclass
class OnlineSearchConfig:
    """联网搜索配置（api_url / timeout / max_results 三项可配）。"""

    api_url: str = ""
    timeout: float = 30.0
    max_results: int = 5

    @classmethod
    def from_yaml(cls) -> "OnlineSearchConfig":
        data = _base.load_config_yaml()
        os_ = expand_env_vars(yaml_section(data, ["online_search"]))
        return cls(
            api_url=str(os_.get("api_url") or ""),
            timeout=float_or(
                None,
                os_.get("timeout") if os_.get("timeout") is not None else 30.0,
            ),
            max_results=int_or(
                None,
                os_.get("max_results") if os_.get("max_results") is not None else 5,
            ),
        )


def get_online_search_config() -> OnlineSearchConfig:
    """返回联网搜索工具配置（每次读取最新 YAML / 环境值）。"""
    return OnlineSearchConfig.from_yaml()


__all__ = ["OnlineSearchConfig", "get_online_search_config"]
