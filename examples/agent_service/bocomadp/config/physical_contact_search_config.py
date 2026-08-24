# -*- coding: utf-8 -*-
"""物理系统负责人查询工具（physical_contact_search）配置模块。

从 ``config.yaml`` 的 ``physical_contact_search`` 节点（扁平结构）构建。
headers 采用"默认头 + 配置合并"：配置中的键覆盖默认头同名键。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import base as _base
from .base import expand_env_vars, float_or, str_dict, yaml_section

DEFAULT_PHYSICAL_CONTACT_SEARCH_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "User-Agent": "DeerFlow-PhysicalContactSearch/2.0",
    "Content-Type": "application/json",
}


@dataclass
class PhysicalContactSearchConfig:
    """物理系统负责人查询配置（api_url + timeout + headers）。"""

    api_url: str = ""
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls) -> "PhysicalContactSearchConfig":
        data = _base.load_config_yaml()
        pcs = expand_env_vars(yaml_section(data, ["physical_contact_search"]))
        headers = dict(DEFAULT_PHYSICAL_CONTACT_SEARCH_HEADERS)
        headers.update(str_dict(pcs.get("headers", {})))
        return cls(
            api_url=str(pcs.get("api_url") or ""),
            timeout=float_or(
                None,
                pcs.get("timeout") if pcs.get("timeout") is not None else 30.0,
            ),
            headers=headers,
        )


def get_physical_contact_search_config() -> PhysicalContactSearchConfig:
    """返回物理系统负责人查询工具配置（每次读取最新 YAML / 环境值）。"""
    return PhysicalContactSearchConfig.from_yaml()


__all__ = [
    "DEFAULT_PHYSICAL_CONTACT_SEARCH_HEADERS",
    "PhysicalContactSearchConfig",
    "get_physical_contact_search_config",
]
