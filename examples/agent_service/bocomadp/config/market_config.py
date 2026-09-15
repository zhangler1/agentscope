# -*- coding: utf-8 -*-
"""智能体市场（agent_market）配置读取。

对应 ``config.yaml`` 的 ``agent_market`` 段::

    agent_market:
      platform_user_id: "default"   # 平台应用智能体统一归属的 user 名下
      default_tag: "未分类"          # 默认标签：创建时自动写入，清标/下架重置回它

读取优先级同 ``bocomadp.config.base``：
环境变量 > .env > config.yaml > 代码默认值。

标签语义：平台智能体创建时自动建立市场档案（启动扫描兜底），
``tag`` 恒不为空；清标/下架均重置回默认标签。打标接口全开放，
标签为自由字符串、无预设清单。
"""
from __future__ import annotations

from typing import Any

from bocomadp.config.base import (
    expand_env_vars,
    load_config_yaml,
    yaml_section,
    yaml_val,
)

# 代码默认值（config.yaml 缺失该段时兜底）
DEFAULT_PLATFORM_USER_ID = "default"
DEFAULT_TAG = "未分类"

_SECTION = ["agent_market"]


def _section() -> dict[str, Any]:
    """取 config.yaml 的 agent_market 段（缺失返回 {}）。"""
    return yaml_section(load_config_yaml(), _SECTION)


def get_platform_user_id() -> str:
    """平台应用智能体归属的 user_id（市场查询范围）。"""
    value = yaml_val(_section(), ["platform_user_id"], DEFAULT_PLATFORM_USER_ID)
    return str(expand_env_vars(value) or DEFAULT_PLATFORM_USER_ID)


def get_default_tag() -> str:
    """前端展示兜底标签（未打标智能体前端可用它占位）。"""
    value = yaml_val(_section(), ["default_tag"], DEFAULT_TAG)
    return str(expand_env_vars(value) or DEFAULT_TAG)


__all__ = [
    "DEFAULT_PLATFORM_USER_ID",
    "DEFAULT_TAG",
    "get_default_tag",
    "get_platform_user_id",
]
