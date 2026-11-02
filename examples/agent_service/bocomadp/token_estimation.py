# -*- coding: utf-8 -*-
"""Token 估算系数：真源在 ``runtime_configs`` 表，库中无记录用代码默认值。

与 ``summarization`` / ``tool_result`` 等配置段一致，配置放在 PG 的
``runtime_configs`` 表（``config_key = "token_estimation"``），可经通用接口
热更新::

    GET    /api/config/token_estimation
    PUT    /api/config/token_estimation   {"chars_per_chinese_token": 1.7, ...}
    DELETE /api/config/token_estimation   # 删除后回退代码默认值

特点：

- **不读 config.yaml**：默认值只存在于本模块的 :class:`TokenEstimationConfig`；
- 表中无记录 / DB 不可用 / 反序列化失败 → 直接使用默认值，不打断对话；
- 进程内 10 秒 TTL 缓存：配置改动最迟 10 秒生效，避免每次模型调用查库。
"""
from __future__ import annotations

import logging
import time

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CONFIG_KEY = "token_estimation"
"""``runtime_configs`` 表中的配置段 key（与 REST 路径 ``/api/config/{key}`` 一致）。"""

_CACHE_TTL_SECS: float = 10.0
"""进程内缓存时长（秒）；到期后重新读库，兼顾热更新与查询开销。"""


class TokenEstimationConfig(BaseModel):
    """Token 估算系数（逐字符分三类后加权求和）。

    估算公式::

        tokens = 中文字数 / chars_per_chinese_token
               + 英文数字字符数 / chars_per_english_token
               + 符号空白字符数 / chars_per_symbol_token
               + 多模态 block 固定估值

    "全部文本"包含所有消息的可见文本与工具 JSON schema；字符数是
    ``len(str)``（非 UTF-8 字节数）。中文含汉字与中文标点；英文数字为
    ASCII 字母与 0-9；其余（JSON 符号、空格、换行、emoji）归入 symbol。
    """

    chars_per_chinese_token: float = Field(
        default=1.7,
        gt=0,
        description="汉字与中文标点：约 1.7 字/token（实测 0.585 token/字）。",
    )
    chars_per_english_token: float = Field(
        default=4.0,
        gt=0,
        description="ASCII 字母与数字：约 4 字符/token。",
    )
    chars_per_symbol_token: float = Field(
        default=2.0,
        gt=0,
        description="其余符号与空白（JSON/代码/换行）：约 2 字符/token。",
    )


_cache: tuple[float, TokenEstimationConfig] | None = None
"""``(monotonic_ts, config)``，进程内全局单例，所有模型实例/会话共享。

注意：写回的值可能是**默认值**（库中无记录 / DB 读取失败时同样缓存一个
TTL），因此 DB 恢复后最长需一个 TTL 才会读到库里的配置。
"""


async def get_token_estimation_config(
    *,
    force: bool = False,
) -> TokenEstimationConfig:
    """读取 token 估算系数（DB 优先，无记录用默认值）。

    Args:
        force (`bool`, optional):
            为 ``True`` 时跳过进程内缓存，强制重新读库（测试/诊断用）。

    Returns:
        `TokenEstimationConfig`: 三个分类系数（中文 / 英文数字 / 符号）；
        库中无记录或读取失败时返回默认值。
    """
    global _cache

    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < _CACHE_TTL_SECS:
        return _cache[1]

    cfg: TokenEstimationConfig | None = None
    try:
        from .runtime_config_store import get_typed_config

        cfg = await get_typed_config(CONFIG_KEY, TokenEstimationConfig)
    except Exception:  # pylint: disable=broad-except
        # DB 不可用：静默回退默认值，不打断对话
        logger.warning(
            "token_estimation: read runtime_configs failed, use defaults",
            exc_info=True,
        )
        cfg = None

    if cfg is None:
        cfg = TokenEstimationConfig()

    _cache = (now, cfg)
    return cfg


def reset_cache() -> None:
    """清空进程内缓存（配置被外部改写后的测试场景使用）。"""
    global _cache
    _cache = None


__all__ = [
    "CONFIG_KEY",
    "TokenEstimationConfig",
    "get_token_estimation_config",
    "reset_cache",
]
