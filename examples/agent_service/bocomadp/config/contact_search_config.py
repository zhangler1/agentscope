# -*- coding: utf-8 -*-
"""通讯录查询工具（contact_search）配置模块。

从 ``config.yaml`` 的 ``contact_search`` 节点（扁平结构）构建。
headers 采用"默认头 + 配置合并"：配置中的键覆盖默认头同名键。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import base as _base
from .base import expand_env_vars, float_or, str_dict, yaml_section

DEFAULT_CONTACT_SEARCH_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "User-Agent": "DeerFlow-ContactSearch/2.0",
    "Content-Type": "application/json",
}


@dataclass
class ContactSearchConfig:
    """通讯录查询配置（api_url + timeout + headers）。"""

    api_url: str = ""
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls) -> "ContactSearchConfig":
        data = _base.load_config_yaml()
        cs = expand_env_vars(yaml_section(data, ["contact_search"]))
        headers = dict(DEFAULT_CONTACT_SEARCH_HEADERS)
        headers.update(str_dict(cs.get("headers", {})))
        return cls(
            api_url=str(cs.get("api_url") or ""),
            timeout=float_or(
                None,
                cs.get("timeout") if cs.get("timeout") is not None else 30.0,
            ),
            headers=headers,
        )


def get_contact_search_config() -> ContactSearchConfig:
    """返回通讯录查询工具配置（每次读取最新 YAML / 环境值）。"""
    return ContactSearchConfig.from_yaml()


__all__ = [
    "DEFAULT_CONTACT_SEARCH_HEADERS",
    "ContactSearchConfig",
    "get_contact_search_config",
]
