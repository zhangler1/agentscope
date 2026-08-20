# -*- coding: utf-8 -*-
"""行内搜索工具（vector_search）配置模块。

从 ``config.yaml`` 的 ``vector_search`` 节点（扁平结构）构建。仅保留
设计确认的 6 项可配（api_url / timeout / page_size / text_top_n /
vector_top_n / space_codes）；其余 13 项固定为代码常量（见
``bocomadp/tools/vector_search.py``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import base as _base
from .base import (
    expand_env_vars,
    float_or,
    int_or,
    split_list,
    yaml_section,
)

DEFAULT_SPACE_CODE_LIST: list[str] = ["SP0999999"]


@dataclass
class VectorSearchConfig:
    """行内搜索配置（6 项可配；请求体参数由运行时 source_param 接管）。"""

    api_url: str = ""
    timeout: float = 30.0
    page_size: int = 10
    text_top_n: int = 7
    vector_top_n: int = 10
    space_codes: list[str] = field(
        default_factory=lambda: list(DEFAULT_SPACE_CODE_LIST),
    )

    @classmethod
    def from_yaml(cls) -> "VectorSearchConfig":
        data = _base.load_config_yaml()
        vs = expand_env_vars(yaml_section(data, ["vector_search"]))
        return cls(
            api_url=str(vs.get("api_url") or ""),
            timeout=float_or(
                None,
                vs.get("timeout") if vs.get("timeout") is not None else 30.0,
            ),
            page_size=int_or(
                None,
                vs.get("page_size") if vs.get("page_size") is not None else 10,
            ),
            text_top_n=int_or(
                None,
                vs.get("text_top_n") if vs.get("text_top_n") is not None else 7,
            ),
            vector_top_n=int_or(
                None,
                vs.get("vector_top_n") if vs.get("vector_top_n") is not None else 10,
            ),
            space_codes=split_list(
                vs.get("space_codes")
                if vs.get("space_codes") is not None
                else DEFAULT_SPACE_CODE_LIST,
            ),
        )


def get_vector_search_config() -> VectorSearchConfig:
    """返回行内搜索工具配置（每次读取最新 YAML / 环境值）。"""
    return VectorSearchConfig.from_yaml()


__all__ = [
    "DEFAULT_SPACE_CODE_LIST",
    "VectorSearchConfig",
    "get_vector_search_config",
]
