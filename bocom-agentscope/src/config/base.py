# -*- coding: utf-8 -*-
"""公共配置加载层（轻量化，环境变量驱动）。

职责：
- 环境变量读取辅助（``_env`` / ``_env_int`` / ``_env_float``）；
- 文档说明读取优先级。

读取优先级（高 → 低）：

    ① 进程环境变量（os.environ）
    ② 宿主应用加载的 ``.env``（本包不主动加载，保持职责单一）
    ③ 代码默认值（最低）

``int`` / ``float`` 转换失败时回退默认值（对齐 bocomadp ``int_or`` /
``float_or`` 语义），避免非法 env 值导致启动崩溃。
"""
from __future__ import annotations

import os
from typing import Any


def _env(key: str, default: Any) -> Any:
    """读取进程环境变量，未设置或为空串时返回 ``default``。"""
    value = os.environ.get(key)
    if value is None or value == "":
        return default
    return value


def _env_int(key: str, default: int) -> int:
    """读取环境变量并转 int，未设置/空/非法时返回 ``default``。"""
    value = _env(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """读取环境变量并转 float，未设置/空/非法时返回 ``default``。"""
    value = _env(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["_env", "_env_int", "_env_float"]
