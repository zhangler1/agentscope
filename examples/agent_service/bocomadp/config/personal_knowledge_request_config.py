# -*- coding: utf-8 -*-
"""个人知识库全文检索（personal_knowledge_request）工具配置模块。

从 ``config.yaml`` 的 ``personal_knowledge_request`` 节点（扁平结构）构建，
仿照 ``raw_request_config.py`` 的模式。仅外置 2 项（api_url / timeout）；
2 个接口的路径表保留在 ``bocomadp/tools/personal_knowledge_request.py``
的 ``DEFAULT_API_PATHS`` 中——路径属接口契约几乎不变，只有域名需要
跨环境（测试/生产）切换。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import base as _base
from .base import expand_env_vars, float_or, yaml_section

# 默认测试环境联机根地址（与 deerflow 原工程一致）
DEFAULT_API_URL = "http://12.244.167.46"


@dataclass
class PersonalKnowledgeRequestConfig:
    """个人知识库全文检索配置（api_url / timeout）。"""

    api_url: str = DEFAULT_API_URL
    timeout: float = 30.0

    @classmethod
    def from_yaml(cls) -> "PersonalKnowledgeRequestConfig":
        data = _base.load_config_yaml()
        pkr = expand_env_vars(
            yaml_section(data, ["personal_knowledge_request"]),
        )
        return cls(
            api_url=str(pkr.get("api_url") or DEFAULT_API_URL),
            timeout=float_or(
                None,
                pkr.get("timeout") if pkr.get("timeout") is not None else 30.0,
            ),
        )


def get_personal_knowledge_request_config() -> PersonalKnowledgeRequestConfig:
    """返回个人知识库全文检索工具配置（每次读取最新 YAML / 环境值）。"""
    return PersonalKnowledgeRequestConfig.from_yaml()


__all__ = [
    "DEFAULT_API_URL",
    "PersonalKnowledgeRequestConfig",
    "get_personal_knowledge_request_config",
]
