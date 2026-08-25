# -*- coding: utf-8 -*-
"""工具命名辅助：中文名（行内）与英文名（行外）环境变量切换。

背景：工具名按用户要求中文化（对齐 deerflow 原设计，行内模型点名靠
中文名），但行外 DeepSeek 等 API 强校验工具名 ``^[a-zA-Z0-9_-]+$``，
中文名会被 400 拒收（行内网关不校验，可正常使用）。

因此工具名默认中文（生产行为不变）；行外联调 / 部署时设置环境变量
``BOCOMADP_TOOL_ASCII_NAMES=1`` 切换到英文名。
"""
from __future__ import annotations

import os


def tool_name(cn: str, en: str) -> str:
    """按环境变量选择工具名。

    Args:
        cn: 中文工具名（默认，行内网关）。
        en: 英文工具名（``BOCOMADP_TOOL_ASCII_NAMES`` 为真值时启用，
            行外 DeepSeek 等 API 兼容）。

    Returns:
        选中的工具名。
    """
    switch = os.environ.get("BOCOMADP_TOOL_ASCII_NAMES", "")
    return en if switch.strip().lower() in ("1", "true", "yes", "on") else cn


__all__ = ["tool_name"]
