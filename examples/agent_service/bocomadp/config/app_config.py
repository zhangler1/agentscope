# -*- coding: utf-8 -*-
"""框架级配置 AppConfig（config.yaml 主源 + 环境变量部署期覆盖）。

> ``config.yaml`` 为唯一配置载体，``AppConfig`` 为唯一 schema：
>
> - ``config.yaml`` 中的全部节点（``log_level`` / ``logging`` / ``service`` /
>   ``redis`` / ``tools`` / ``middlewares`` / ``mcp`` /
>   ``providers`` / ``app_name`` / ``workspace_dir``）作为主配置源；
> - ``BOCOMADP_`` 前缀环境变量 / ``.env`` 文件可在部署期覆盖（优先级更高）。
>
> 读取优先级（高 → 低，由 pydantic-settings 保证）：
> 进程环境变量 > ``.env`` 文件 > ``config.yaml``（$VAR 展开后）> 代码默认值。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

import difflib
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import YamlConfigSettingsSource

from .base import (
    BASE_DIR,
    CONFIG_YAML_FILE,
    DOTENV_FILE,
    expand_env_vars,
    load_config_yaml,
    resolve_path,
)

# 顶层业务节点白名单：这些键**有意**不在 AppConfig schema 内，
# 由独立读取器消费（models → load_models_from_yaml；
# cross_search → get_cross_search_config）。
# 新增此类业务节点时，必须加入本集合，否则启动校验会 fail-fast。
_BUSINESS_KEYS: frozenset[str] = frozenset(
    {
        "models",
        "audit",
        "contact_search",
        "cross_search",
        "online_search",
        "personal_search",
        "physical_contact_search",
        "vector_search",
        "raw_request",
        "rate_currency",
        "uploads",
        "agents",
    },
)

# 拼写校验相似度阈值：YAML 键与声明字段的相似度达到该值即视为疑似拼写错误。
_SPELL_CHECK_CUTOFF = 0.7


def _reject_unknown_yaml_keys(data: dict[str, Any]) -> None:
    """递归校验 YAML 键与 schema 字段匹配，发现疑似拼写错误时抛错。

    - 顶层：白名单（``_BUSINESS_KEYS``）之外的未知键，用 ``difflib`` 与声明
      字段做模糊匹配，高度相似视为拼写错误（fail-fast，而非静默用默认值）；
    - 嵌套：对声明为 ``BaseModel`` 子类的字段递归校验其子键。
    """

    def _walk(data: dict[str, Any], fields: dict, path: str = "") -> None:
        for key in data:
            if key in fields:
                # 递归下钻嵌套模型
                annotation = fields[key].annotation
                if (
                    isinstance(data[key], dict)
                    and isinstance(annotation, type)
                    and issubclass(annotation, BaseModel)
                    and getattr(annotation, "model_fields", None)
                ):
                    _walk(data[key], annotation.model_fields, f"{path}{key}.")
                continue
            if not path and key in _BUSINESS_KEYS:
                continue
            match = difflib.get_close_matches(
                str(key),
                [f for f in fields if f not in _BUSINESS_KEYS],
                n=1,
                cutoff=_SPELL_CHECK_CUTOFF,
            )
            hint = f"，是否应为 {match[0]!r}？" if match else ""
            raise ValueError(
                f"config.yaml 中键 {path + str(key)!r} 未在 "
                f"AppConfig schema 中声明{hint}；"
                "请检查拼写；若为有意新增的业务节点，"
                "请将其加入 bocomadp.config.app_config._BUSINESS_KEYS 白名单。",
            )

    _walk(data, AppConfig.model_fields)


def get_app_config() -> "AppConfig":
    """加载最新应用配置：每次调用重建 ``AppConfig``（config.yaml 实时重读 = 热加载）。

    适合**运行时**按需获取最新配置（如每次请求 / 每次 agent 组装）；
    启动装配（注册表 / Redis / 中间件）用一次快照即可，保持装配一致性。
    """
    return AppConfig()


class LoggingEnhanceConfig(BaseModel):
    """Request trace logging enhancement — drives :func:`configure_logging`."""

    enabled: bool = Field(
        default=True,
        description="Install TraceContextFilter + trace formatter on root handlers.",
    )
    format: Literal["text", "json"] = Field(
        default="text",
        description="Enhanced log output format. JSON is recommended for prod.",
    )


class LoggingConfig(BaseModel):
    """Logging configuration."""

    enhance: LoggingEnhanceConfig = Field(default_factory=LoggingEnhanceConfig)


class ServiceConfig(BaseModel):
    """HTTP server settings."""

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    reload: bool = Field(default=False, description="Enable uvicorn auto-reload (dev only).")


class RedisConfig(BaseModel):
    """Redis backend for AgentScope storage / message bus.

    ``main.py`` 中 ``RedisStorage`` 应通过 ``config.redis.host`` /
    ``config.redis.port`` 读取，禁止绕过本配置直接 ``os.getenv``。
    """

    host: str = Field(default="localhost")
    port: int = Field(default=6379)
    # Redis 连接池上限（redis-py 默认仅 100，压测/生产建议调大）。
    max_connections: int = Field(
        default=200,
        description="Redis 连接池上限。",
    )


class RunConcurrencyConfig(BaseModel):
    """/chat 并发控制上限(仿 deer-flow run_concurrency)。

    ``0`` 表示不限;``grace_secs`` 为装配窗口宽限秒数;``enabled=false``
    时中间件完全透传,跳过对账/占位/注册。
    """

    enabled: bool = Field(
        default=True,
        description="并发控制总开关;false 时中间件完全透传,跳过对账/占位/注册。",
    )
    max_running: int = Field(
        default=10,
        ge=0,
        description="全局并发上限(0=不限)。",
    )
    max_running_per_user: int = Field(
        default=3,
        ge=0,
        description="每用户并发上限(0=不限)。",
    )
    grace_secs: float = Field(
        default=6.0,
        gt=0,
        description="装配窗口宽限秒数(响应先于框架锁 key 创建的时间窗口)。",
    )


class EllmKeyRefreshConfig(BaseModel):
    """ELLM apikey 刷新策略配置。

    ``refresh_ahead_secs`` 为**提前刷新窗口**：在网关下发的 key 真正
    过期之前就提前换新，留出网关抖动/网络异常的缓冲，避免拿一个
    已失效的 key 去请求（旧逻辑只在 ``now > apikey_expires_at``
    时才刷新，过期边界上一旦刷新失败即带死 key 请求）。

    取值约定：``0`` 表示关闭提前刷新（回退到旧行为：硬过期才换）；
    正数表示在过期前 N 秒开始刷新。建议设为 key 有效期的 1/10 左右
    （如有效期 25 分钟则取 120~180s）。
    """

    refresh_ahead_secs: float = Field(
        default=120.0,
        ge=0,
        description=(
            "ELLM key 提前刷新窗口（秒）；0=关闭（硬过期才换），"
            "建议 120~180s。"
        ),
    )


class SummarizationConfig(BaseModel):
    """上下文压缩统一模型配置（config.yaml ``summarization:`` 节点）。

    ``enabled=false``（默认）时压缩中间件纯透传，压缩用各会话自身模型；
    ``enabled=true`` 时 ``user_id`` / ``credential_id`` / ``model_name``
    必填（启动报错）。
    凭证按 ``user_id`` + ``credential_id`` 查库，凭证 ID 无格式约定、
    可任意；代码不做凭证创建，也不从 ID 解析任何字段。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用统一压缩模型（开关的是模型替换，框架压缩本身一直开启）。",
    )
    user_id: str | None = Field(
        default=None,
        description="凭证归属用户（owner）；按 user_id + credential_id 查库。",
    )
    credential_id: str | None = Field(
        default=None,
        description="bocom_ellm 凭证 ID（无格式约定，使用方提供）。",
    )
    model_name: str | None = Field(
        default=None,
        description="ELLM 模型名（凭证不绑定单模型，必配）。",
    )

    @model_validator(mode="after")
    def _validate_fields(self) -> Self:
        if self.enabled and (
            not self.user_id or not self.credential_id or not self.model_name
        ):
            raise ValueError(
                "summarization.enabled=true 时 user_id / credential_id / "
                "model_name 均必填",
            )
        return self


class ImageParseConfig(BaseModel):
    """图片解析统一多模态模型配置（PG ``runtime_configs`` 表 ``view_image`` key）。

    与压缩模型（``SummarizationConfig``）同模式：``enabled=false``（默认）时
    图片解析工具无统一模型可用（返回 None，不再回退 config.yaml）；
    ``enabled=true`` 时 ``user_id`` / ``credential_id`` / ``model_name``
    必填（校验失败按无效配置处理，同样不可用）。
    凭证按 ``user_id`` + ``credential_id`` 查库，代码不做凭证创建、
    也不从 ID 解析任何字段。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用统一多模态模型（图片解析工具专用）。",
    )
    user_id: str | None = Field(
        default=None,
        description="凭证归属用户（owner）；按 user_id + credential_id 查库。",
    )
    credential_id: str | None = Field(
        default=None,
        description="bocom_ellm 凭证 ID（无格式约定，使用方提供）。",
    )
    model_name: str | None = Field(
        default=None,
        description="ELLM 多模态模型名（凭证不绑定单模型，必配）。",
    )

    @model_validator(mode="after")
    def _validate_fields(self) -> Self:
        if self.enabled and (
            not self.user_id or not self.credential_id or not self.model_name
        ):
            raise ValueError(
                "view_image.enabled=true 时 user_id / credential_id / "
                "model_name 均必填",
            )
        return self


class DbConfig(BaseModel):
    """AgentScope 持久化存储后端（AsyncSQLAlchemyStorage）。

    ``main.py`` 中 ``AsyncSQLAlchemyStorage`` 通过 ``config.db.url`` 读取，
    禁止绕过本配置直接 ``os.getenv``。支持任意 SQLAlchemy async URL：
    ``postgresql+asyncpg://`` / ``sqlite+aiosqlite://`` / ``mysql+aiomysql://``。
    """

    url: str = Field(
        default=(
            "postgresql+asyncpg://agentscope:agentscope"
            "@localhost:5432/agentscope"
        ),
        description="SQLAlchemy async URL；AgentScope StorageBase 的关系库后端。",
    )
    create_tables: bool = Field(
        default=True,
        description=(
            "启动时 Base.metadata.create_all 自动建表，dev/单机够用；"
            "多副本生产应关闭，改用离线 alembic upgrade head 避免迁移竞争。"
        ),
    )
    pool_pre_ping: bool = Field(
        default=True,
        description=(
            "取出连接前先探测存活（asyncpg ping），"
            "服务端/网络静默断连时自动重建，避免 connection is closed。"
        ),
    )
    pool_recycle: int = Field(
        default=1800,
        description=(
            "空闲连接回收秒数（默认 30 分钟），"
            "早于防火墙/NAT 空闲超时主动重建连接。"
        ),
    )


# ---------------------------------------------------------------------------
# PORT-FROM-QWENPAW placeholders
# ---------------------------------------------------------------------------
# Each is a minimal stub. Flip ``enabled`` to True and wire the module into
# main.py once you've migrated it from QwenPaw. Keep them disabled by default
# so the skeleton stays runnable without the dependency.


class ModelEntry(BaseModel):
    """config.yaml 中单个模型 Provider 条目。

    启动时由 ``load_models_from_yaml`` 读取，通过 ``CredentialFactory``
    动态实例化 credential + model，注册到 ``ProviderManager``。
    """

    provider_id: str = Field(description="唯一标识，如 deepseek")
    display_name: str = Field(default="", description="前端显示名")
    provider_type: str = Field(
        default="deepseek",
        description=(
            "凭证类型，对应 CredentialFactory 中的 type 前缀："
            "deepseek / openai / anthropic / dashscope / gemini "
            "/ ollama / moonshot / xai"
        ),
    )
    model_name: str = Field(default="", description="模型名，如 deepseek-chat")
    api_key: str = Field(default="", description="API Key，支持 ${ENV_VAR} 语法")
    base_url: str = Field(default="", description="API base URL，留空用默认")
    is_active: bool = Field(default=False, description="是否设为活跃模型")
    supports_multimodal: bool = Field(default=False, description="是否支持多模态")
    supports_thinking: bool = Field(
        default=False,
        description="是否支持 thinking 模式（deer-flow 前端据此展示开关）",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="透传给 ChatModel.Parameters 的额外参数",
    )


class ProviderConfig(BaseModel):
    """模型 Provider 路由配置。

    ``config_file`` 指向 YAML 文件，启动时自动加载并注册到
    ``ProviderManager``。文件不存在时跳过（不影响启动）。
    """

    enabled: bool = Field(
        default=True,
        description="启动时从 config.yaml 加载并注册模型。",
    )
    config_file: str | Path | None = Field(
        default=None,
        description="模型 Provider 配置文件路径；None 时使用 agent_service/config.yaml。",
    )
    manager_class: str = Field(
        default="bocomadp.providers.ProviderManager",
        description="Dotted path to the provider manager (set after migration).",
    )


class GovernanceConfig(BaseModel):
    """PORT-FROM-QWENPAW: ``qwenpaw/governance/``.

    Agent-level governance (doom-loop gates, budget gates, rubric gates).
    Heavy (~5k lines). Only migrate if you need agent-loop safety rails.
    """

    enabled: bool = False


class HooksConfig(BaseModel):
    """PORT-FROM-QWENPAW: ``qwenpaw/hooks/``.

    Runtime-level hooks (error_hook, etc.). Migrate per-hook; each hook is
    a small module implementing the agentscope Hook protocol.
    """

    enabled: bool = False


class CheckpointsConfig(BaseModel):
    """PORT-FROM-QWENPAW: ``qwenpaw/checkpoints/``.

    Conversation checkpointing / branching. Migrate if you need session
    history replay and branching UI.
    """

    enabled: bool = False
    storage_dir: str = Field(default="./data/checkpoints")


class TokenUsageConfig(BaseModel):
    """PORT-FROM-QWENPAW: ``qwenpaw/token_usage/``.

    Per-turn / per-session token accounting. Lightweight (~1k lines) and
    self-contained — a good early migration candidate.
    """

    enabled: bool = False


class LocalModelsConfig(BaseModel):
    """PORT-FROM-QWENPAW: ``qwenpaw/local_models/``.

    Local model lifecycle (Ollama-style). Migrate only if you serve local
    models from this service.
    """

    enabled: bool = False


# ---------------------------------------------------------------------------
# Tools / middleware config (new framework modules)
# ---------------------------------------------------------------------------


class ToolsConfig(BaseModel):
    """Configuration for the custom tool registry."""

    enabled: bool = Field(
        default=True,
        description="Load built-in custom tools into every agent.",
    )
    load_custom: bool = Field(
        default=True,
        description="自动扫描 tools/custom/ 下的 @tool 函数。",
    )


class MiddlewaresConfig(BaseModel):
    """配置 agent 级中间件注册表。"""

    enabled: bool = Field(
        default=True,
        description="加载 agent_middleware.py 中的内置中间件。",
    )
    load_custom: bool = Field(
        default=True,
        description="自动扫描 middleware/custom/ 下的 Middleware 实例。",
    )


class McpConfig(BaseModel):
    """配置 MCP 注册表。"""

    enabled: bool = Field(
        default=True,
        description="加载 builtin_mcps.py 中的 MCPClient 实例。",
    )
    load_custom: bool = Field(
        default=True,
        description="自动扫描 mcp/custom/ 下的 MCPClient 实例。",
    )


class _ExpandedYamlSource(YamlConfigSettingsSource):
    """config.yaml 配置源：读取后先做 ``$VAR`` / ``${VAR}`` 环境变量展开，
    再做键拼写校验（fail-fast）。

    继承 ``YamlConfigSettingsSource``（其 ``get_field_value`` 支持嵌套模型
    按字段名递归填充），仅在解析前多一步环境变量展开，保证与
    ``load_config_yaml`` / ``expand_env_vars`` 的行为一致。
    键校验与读取同源（仅一次文件读取，无 lru_cache），修改文件内容后
    校验实时生效，与热加载语义一致。
    """

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        data = super()._read_file(file_path)
        data = expand_env_vars(data) if data else data
        # 对必填项开启校验：拼写错误（如 redis.hots）不再静默忽略
        # （extra="ignore" 仅用于容忍业务节点），fail-fast 暴露问题。
        if data:
            _reject_unknown_yaml_keys(data)
        return data


class AppConfig(BaseSettings):
    """应用配置：config.yaml 为主源，BOCOMADP_* 环境变量部署期覆盖。

    优先级（高 → 低）：

    1. 进程环境变量（``BOCOMADP_`` 前缀）
    2. ``agent_service/.env`` 中的 ``BOCOMADP_*`` 键
    3. ``agent_service/config.yaml``（``$VAR`` 展开后）—— 主配置源
    4. 代码默认值

    嵌套字段用 ``__`` 分隔，例如::

        BOCOMADP_LOG_LEVEL=debug
        BOCOMADP_LOGGING__ENHANCE__ENABLED=true
        BOCOMADP_SERVICE__PORT=9000
        BOCOMADP_REDIS__HOST=redis.local
    """

    model_config = SettingsConfigDict(
        env_prefix="BOCOMADP_",
        env_file=DOTENV_FILE,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """把 ``config.yaml``（含 $VAR 展开）作为主源插入优先级链。

        source 顺序即优先级：init > 进程环境变量 > .env > config.yaml > secrets。
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _ExpandedYamlSource(settings_cls, yaml_file=CONFIG_YAML_FILE),
            file_secret_settings,
        )

    # ---- 全局（config.yaml 根节点） ----
    app_name: str = Field(default="交通银行智能体平台")
    workspace_dir: Path = Field(
        default_factory=lambda: BASE_DIR / "workspaces",
        description="工作区目录（AgentScope 沙箱文件读写根目录），相对路径基于 BASE_DIR 归一化。",
    )

    @field_validator("workspace_dir", mode="before")
    @classmethod
    def _normalize_workspace_dir(cls, v: Any) -> Path:
        """将 workspace_dir 归一化为绝对路径（相对路径基于 BASE_DIR 解析）。

        与启动工作目录无关：无论从哪个目录启动，路径都一致指向
        ``agent_service/`` 下的目标目录。
        """
        if v is None or v == "":
            return BASE_DIR / "workspaces"
        return resolve_path(v)

    # ---- core ----
    log_level: str = Field(default="info")
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    ellm_key_refresh: EllmKeyRefreshConfig = Field(
        default_factory=EllmKeyRefreshConfig,
        description="ELLM apikey 提前刷新窗口配置。",
    )
    summarization: SummarizationConfig = Field(
        default_factory=SummarizationConfig,
        description="上下文压缩统一模型配置（summarization: 节点）。",
    )
    run_concurrency: RunConcurrencyConfig = Field(
        default_factory=RunConcurrencyConfig,
        description="/chat 并发控制配置。",
    )

    # ---- QwenPaw migration placeholders (all default off) ----
    providers: ProviderConfig = Field(default_factory=ProviderConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    checkpoints: CheckpointsConfig = Field(default_factory=CheckpointsConfig)
    token_usage: TokenUsageConfig = Field(default_factory=TokenUsageConfig)
    local_models: LocalModelsConfig = Field(default_factory=LocalModelsConfig)

    # ---- New framework modules ----
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    middlewares: MiddlewaresConfig = Field(default_factory=MiddlewaresConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


def load_models_from_yaml(
    path: str | Path | None = None,
) -> list[ModelEntry]:
    """从 YAML 文件加载模型 Provider 列表。

    默认读取 ``agent_service/config.yaml``（绝对路径，与启动工作目录无关），
    读取后先做 ``$VAR`` / ``${VAR}`` 环境变量展开。文件不存在时返回空列表
    （不影响启动）。
    """
    if path:
        p = Path(path)
        if not p.exists():
            return []
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data = expand_env_vars(raw) if isinstance(raw, dict) else {}
    else:
        data = expand_env_vars(load_config_yaml())
    entries_data = data.get("models", [])
    result: list[ModelEntry] = []
    for item in entries_data:
        result.append(ModelEntry(**item))
    return result


def build_model_instance(entry: ModelEntry):
    """根据 ModelEntry 动态创建 ChatModel 实例。

    利用 ``CredentialFactory`` 按 ``provider_type`` 查找 credential
    类，实例化后通过 ``get_chat_model_class()`` 获取对应的
    ``ChatModelBase`` 子类并构造模型。
    """
    from agentscope.credential import CredentialFactory

    # 先用简写匹配（如 deepseek），失败则补 _credential 后缀再试
    credential_cls = CredentialFactory.get_credential_class(
        entry.provider_type,
    )
    if credential_cls is None and not entry.provider_type.endswith("_credential"):
        credential_cls = CredentialFactory.get_credential_class(
            f"{entry.provider_type}_credential",
        )
    if credential_cls is None:
        raise ValueError(
            f"Unknown provider_type: {entry.provider_type!r} "
            f"(provider_id={entry.provider_id})",
        )

    credential_kwargs: dict[str, Any] = {"api_key": entry.api_key}
    # 仅当 credential 类有 base_url 字段时才传入
    if entry.base_url and "base_url" in credential_cls.model_fields:
        credential_kwargs["base_url"] = entry.base_url
    # 仅当 credential 类有 model 字段时才传入（如 ELLMCredential 必填）
    if "model" in credential_cls.model_fields:
        credential_kwargs["model"] = entry.model_name or entry.provider_id
    credential = credential_cls(**credential_kwargs)

    model_cls = credential_cls.get_chat_model_class()
    # AgentScope 的 ChatModel 期望 Parameters（pydantic 模型）而非 dict；
    # 将 config.yaml 的 parameters 字典转换为模型类对应的 Parameters 对象，
    # 否则调用阶段会报 'dict' object has no attribute 'max_tokens'。
    parameters = entry.parameters or None
    if parameters is not None:
        parameters = model_cls.Parameters(**parameters)
    model = model_cls(
        credential=credential,
        model=entry.model_name or entry.provider_id,
        parameters=parameters,
    )
    return model


def is_trace_correlation_enabled(config: AppConfig) -> bool:
    """Single source of truth for the trace-correlation gate.

    Used by both the TraceMiddleware (ASGI) and ``configure_logging`` so
    they cannot drift on when ``trace_id`` is bound.
    """
    return bool(
        getattr(config.logging, "enhance", None)
        and config.logging.enhance.enabled
    )
