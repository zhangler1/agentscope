# -*- coding: utf-8 -*-
"""配置包：汇总导出全部配置类与工厂函数（对应设计文档 ``config/__init__.py``）。

业务代码统一从 ``bocomadp.config`` 导入，如::

    from bocomadp.config import get_app_config

单源化配置体系（``config.yaml`` 为唯一配置载体）：

1. :class:`AppConfig`（pydantic-settings，config.yaml 主源 + ``BOCOMADP_`` env 覆盖）：
   - 框架级 —— 日志 / 服务 / Redis / 三大注册表开关 / providers
   - 全局根节点 —— ``app_name`` / ``workspace_dir``
"""
from .base import (
    BASE_DIR,
    CONFIG_YAML_FILE,
    DOTENV_FILE,
    _load_dotenv_once,
    expand_env_vars,
    load_config_yaml,
    resolve_path,
    yaml_section,
    yaml_val,
)
from .uploads_config import UploadConfig, VIRTUAL_PATH_PREFIX, get_upload_config
from .app_config import (
    AppConfig,
    CheckpointsConfig,
    SummarizationConfig,
    ImageParseConfig,
    GovernanceConfig,
    HooksConfig,
    LocalModelsConfig,
    LoggingConfig,
    LoggingEnhanceConfig,
    McpConfig,
    MiddlewaresConfig,
    ModelEntry,
    ProviderConfig,
    RedisConfig,
    ServiceConfig,
    TokenUsageConfig,
    ToolsConfig,
    build_model_instance,
    get_app_config,
    is_trace_correlation_enabled,
    load_models_from_yaml,
)

__all__ = [
    # base.py —— 公共加载层
    "BASE_DIR",
    "CONFIG_YAML_FILE",
    "DOTENV_FILE",
    "_load_dotenv_once",
    "expand_env_vars",
    "load_config_yaml",
    "resolve_path",
    "yaml_section",
    "yaml_val",
    # uploads_config.py —— 文件上传配置
    "UploadConfig",
    "VIRTUAL_PATH_PREFIX",
    "get_upload_config",
    # app_config.py —— 单源化配置（config.yaml 主源 + env 覆盖）
    "AppConfig",
    "CheckpointsConfig",
    "SummarizationConfig",
    "ImageParseConfig",
    "GovernanceConfig",
    "HooksConfig",
    "LocalModelsConfig",
    "LoggingConfig",
    "LoggingEnhanceConfig",
    "McpConfig",
    "MiddlewaresConfig",
    "ModelEntry",
    "ProviderConfig",
    "RedisConfig",
    "ServiceConfig",
    "TokenUsageConfig",
    "ToolsConfig",
    "build_model_instance",
    "get_app_config",
    "is_trace_correlation_enabled",
    "load_models_from_yaml",
]
