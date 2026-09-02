# -*- coding: utf-8 -*-
"""行内模型平台（ELLM）配置：环境变量驱动，每次调用重建、热读。

- :class:`EllmSettings`：frozen dataclass，字段对应 bocomadp 配置中
  ``redis``（模型表/会话覆盖存储）与 ``ellm_key_refresh`` 两节；
- :func:`get_ellm_settings`：每次调用重建、env 热读，部署时环境变量变更
  即时生效，无需重启。

字段与默认值：

======================  ===========================  ========================
字段                    环境变量                      默认值
======================  ===========================  ========================
``redis_host``          ``ELLM_REDIS_HOST``           ``localhost``
``redis_port``          ``ELLM_REDIS_PORT``           ``6379``
``redis_timeout``       ``ELLM_REDIS_TIMEOUT``        ``1.0``
``redis_max_connections`` ``ELLM_REDIS_MAX_CONNECTIONS`` ``200``
``model_think_tag_key`` ``ELLM_MODEL_THINK_TAG_KEY``  ``bocomadp:model:think_tag``
``refresh_ahead_secs``  ``ELLM_KEY_REFRESH_AHEAD_SECS`` ``120.0``
======================  ===========================  ========================

``model_think_tag_key`` 默认值与既有模型表 key 一致，保证现有数据无缝兼容。
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import _env, _env_float, _env_int


@dataclass(frozen=True)
class EllmSettings:
    """行内模型平台（ELLM）运行参数快照。"""

    #: 模型表 / 会话级覆盖所在的 Redis
    redis_host: str
    redis_port: int
    redis_timeout: float
    redis_max_connections: int
    #: Redis 模型表 key（field=模型名，value=JSON {think_tag, context_size, output_size}）
    model_think_tag_key: str
    #: ELLM apikey 提前刷新窗口（秒）
    refresh_ahead_secs: float


def get_ellm_settings() -> EllmSettings:
    """每次调用重建、env 热读的 ELLM 配置快照。"""
    return EllmSettings(
        redis_host=str(_env("ELLM_REDIS_HOST", "localhost")),
        redis_port=_env_int("ELLM_REDIS_PORT", 6379),
        redis_timeout=_env_float("ELLM_REDIS_TIMEOUT", 1.0),
        redis_max_connections=_env_int("ELLM_REDIS_MAX_CONNECTIONS", 200),
        model_think_tag_key=str(
            _env("ELLM_MODEL_THINK_TAG_KEY", "bocomadp:model:think_tag"),
        ),
        refresh_ahead_secs=_env_float("ELLM_KEY_REFRESH_AHEAD_SECS", 120.0),
    )


__all__ = ["EllmSettings", "get_ellm_settings"]
