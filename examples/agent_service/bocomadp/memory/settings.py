# -*- coding: utf-8 -*-
"""memory 包部署配置（``MemorySettings``）：由使用方显式构造并注入。

本模型承载的是**部署期**参数：SDK 连接配置（EUVD 记忆平台）+ DB 连接 +
提取行为参数，与另外两个"存库"的模型区分：

- :class:`memory.storage._models.MemoryAgentConfigRecord` —— 单个 agent 的
  记忆配置（存库）；
- :class:`memory.config.MemoryGlobalConfig` —— 全局运行参数（存库）。

本包不读取任何全局配置单例：所有外部依赖（平台 / DB）都由使用方在
``memory.setup_memory(settings=...)`` 时一次性传入。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MemorySettings(BaseModel):
    """memory 包运行所需的外部配置（SDK 配置 + DB 连接）。

    仅承载部署期参数：由使用方在 ``setup_memory`` 时一次性注入，运行期不可改。

    Attributes:
        environment: **必填**——SDK 环境（sit/uat/uat2/prd），原样透传 SDK，
            枚举校验由 SDK 负责（非法值 SDK 直接 raise，fail-fast）。
            不设默认值、也不回退 ``EUVD_MEMORIES_ENV``：环境必须显式声明。
        db_url: 存 ``memory_agent_configs`` / ``memory_session_states``
            两张表的 SQLAlchemy 异步连接串（全局参数另走
            ``runtime_configs`` 表）；注入 ``engine`` 或记忆后端用 Redis
            时可留空——SQL 装配时两者皆空会 fail-fast（见 ``setup_memory``）。
        engine: 可选：外部管理的 SQLAlchemy ``AsyncEngine``。传入则本包不自建
            连接池（生命周期仍归调用方，本包不会 dispose 它）。
        dangling_wait_secs: 提取前"落库确认"的首查延迟（秒）。
        lock_ttl_secs: 提取锁 TTL（``message_bus.acquire_lock``，秒）。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db_url: str = Field(
        default="",
        description="SQL 后端资源入口；注入 engine 时可留空。",
    )
    engine: Any = Field(
        default=None,
        exclude=True,
        description="可选：外部管理的 AsyncEngine（传入则本包不自建连接池）。",
    )

    # --- SDK（MemoriesClientConfig 透传）---
    environment: str = Field(
        min_length=1,
        description="必填：sit/uat/uat2/prd；透传 SDK 校验，非法值 fail-fast。",
    )
    connect_timeout_seconds: float = Field(default=3.0, gt=0)
    read_timeout_seconds: float = Field(default=10.0, gt=0)
    pool_connections: int = Field(default=10, ge=1)
    pool_maxsize: int = Field(default=20, ge=1)
    headers: dict[str, str] = Field(default_factory=dict)

    # --- 提取行为 ---
    dangling_wait_secs: float = Field(
        default=1.0,
        gt=0,
        description="落库确认首查延迟（秒）。",
    )
    lock_ttl_secs: int = Field(
        default=600,
        description="提取锁 TTL（bus.acquire_lock，秒）。",
    )


_settings: MemorySettings | None = None


def configure_settings(settings: MemorySettings) -> None:
    """注入部署配置（由 ``setup_memory`` 调用）。"""
    global _settings
    _settings = settings


def get_settings() -> MemorySettings:
    """返回当前生效的部署配置；未注入 → raise。

    不再提供"默认配置"实例：``environment`` 必填使默认值失去意义，
    静默兜底只会把配置错误推迟到更难排查的位置。
    """
    if _settings is None:
        raise RuntimeError(
            "memory: 部署配置未注入 —— 请先调用 "
            "memory.setup_memory(settings=...)"
        )
    return _settings


def reset_settings() -> None:
    """清空注入的配置（测试用）。"""
    global _settings
    _settings = None


__all__ = [
    "MemorySettings",
    "configure_settings",
    "get_settings",
    "reset_settings",
]
