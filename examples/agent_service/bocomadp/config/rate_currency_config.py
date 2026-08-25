# -*- coding: utf-8 -*-
"""利率汇率工具配置模块（汇率查询 + 利率查询共享）。

从 ``config.yaml`` 的 ``rate_currency`` 节点（扁平结构）构建，仿照
``raw_request_config.py`` 的模式。仅外置 2 项可配（api_url / timeout）；
3 个接口（汇率 / 存款利率 / 贷款利率）的路径表分别保留在
``bocomadp/tools/exchange_rate.py`` 与 ``bocomadp/tools/interest_rate.py``
中——路径属接口契约几乎不变，只有域名需要跨环境（测试/生产）切换。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from .base import expand_env_vars, float_or, yaml_section

# 默认联机根地址（与原 deerflow 工程一致）
DEFAULT_API_URL = "http://12.244.167.46"


@dataclass
class RateCurrencyConfig:
    """利率汇率配置（api_url / timeout）。"""

    api_url: str = DEFAULT_API_URL
    timeout: float = 30.0

    @classmethod
    def from_yaml(cls) -> "RateCurrencyConfig":
        data = _base.load_config_yaml()
        rc = expand_env_vars(yaml_section(data, ["rate_currency"]))
        return cls(
            api_url=str(rc.get("api_url") or DEFAULT_API_URL),
            timeout=float_or(
                None,
                rc.get("timeout") if rc.get("timeout") is not None else 30.0,
            ),
        )


def get_rate_currency_config() -> RateCurrencyConfig:
    """返回利率汇率工具配置（每次读取最新 YAML / 环境值）。"""
    return RateCurrencyConfig.from_yaml()


__all__ = [
    "DEFAULT_API_URL",
    "RateCurrencyConfig",
    "get_rate_currency_config",
]
