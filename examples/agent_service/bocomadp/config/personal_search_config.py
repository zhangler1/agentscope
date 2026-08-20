# -*- coding: utf-8 -*-
"""个人知识库搜索工具（personal_search）配置模块。

从 ``config.yaml`` 的 ``personal_search`` 节点（扁平结构）构建。
headers 采用"默认头 + 配置合并"：配置中的键覆盖默认头同名键。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import base as _base
from .base import expand_env_vars, float_or, str_dict, yaml_section

DEFAULT_PERSONAL_SEARCH_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "User-Agent": "DeerFlow-PersonalSearch/2.0",
    "jumpCloud-Env": "BASE",
}


@dataclass
class PersonalSearchConfig:
    """个人知识库搜索配置（5 键 + headers，空间参数不进配置）。"""

    api_url: str = ""
    timeout: float = 30.0
    source_type: str = "WDZS"
    repository: str = "personal-search"
    search_type: str = "0"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls) -> "PersonalSearchConfig":
        data = _base.load_config_yaml()
        ps = expand_env_vars(yaml_section(data, ["personal_search"]))
        headers = dict(DEFAULT_PERSONAL_SEARCH_HEADERS)
        headers.update(str_dict(ps.get("headers", {})))
        return cls(
            api_url=str(ps.get("api_url") or ""),
            timeout=float_or(
                None,
                ps.get("timeout") if ps.get("timeout") is not None else 30.0,
            ),
            source_type=str(ps.get("source_type") or "WDZS"),
            repository=str(ps.get("repository") or "personal-search"),
            search_type=str(ps.get("search_type") or "0"),
            headers=headers,
        )


def get_personal_search_config() -> PersonalSearchConfig:
    """返回个人知识库搜索工具配置（每次读取最新 YAML / 环境值）。"""
    return PersonalSearchConfig.from_yaml()


__all__ = [
    "DEFAULT_PERSONAL_SEARCH_HEADERS",
    "PersonalSearchConfig",
    "get_personal_search_config",
]
