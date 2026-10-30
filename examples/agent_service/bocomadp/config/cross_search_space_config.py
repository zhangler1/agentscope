# -*- coding: utf-8 -*-
"""空间名称查询网关（跨知识搜索「引用知识空间」登记时查空间名）的配置模块。

从 ``config.yaml`` 的 ``cross_search_space`` 节点（扁平结构）构建，仿照
``raw_request_config.py`` 的模式：仅外置联机地址与超时两项，接口路径
（queryPageKnSpace.do）属接口契约，随地址一并写在 yaml 里；域名跨环境
（测试/生产）切换只改 yaml。

``api_url`` 缺省为空：yaml 未配置该节点时查名直接失败（异常被吞、名字留空，
不阻断引用登记），不会回落到某个环境地址而误连。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from .base import expand_env_vars, float_or, yaml_section


@dataclass
class CrossSearchSpaceConfig:
    """空间名称查询网关配置（api_url / timeout）。"""

    api_url: str = ""
    timeout: float = 20.0

    @classmethod
    def from_yaml(cls) -> "CrossSearchSpaceConfig":
        data = _base.load_config_yaml()
        cs = expand_env_vars(yaml_section(data, ["cross_search_space"]))
        return cls(
            api_url=str(cs.get("api_url") or ""),
            timeout=float_or(
                None,
                cs.get("timeout") if cs.get("timeout") is not None else 20.0,
            ),
        )


def get_cross_search_space_config() -> CrossSearchSpaceConfig:
    """返回空间名称查询网关配置（每次读取最新 YAML / 环境值）。"""
    return CrossSearchSpaceConfig.from_yaml()


__all__ = [
    "CrossSearchSpaceConfig",
    "get_cross_search_space_config",
]
