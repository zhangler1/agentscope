"""deer-flow 模型名对接单测（credentials + 模型解析 + models 端点）。

覆盖计划测试矩阵：

- ``_resolve_requested_model_name``：llm_model_name 单一通道与空回退；
- ``ensure_default_credentials``：default 用户维度、幂等 upsert、失败不阻断；
- ``_resolve_chat_model_config``：模型名透传、用户凭证表挑选
  （ELLM 优先）、默认凭证复制入库、default 凭证缺失回退 config.yaml
  条目；
- ``_prepare_session_for_run``：首次 backfill、模型切换更新、一致不更新。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agentscope.app.storage import ChatModelConfig, SessionConfig
from agentscope.app.storage._utils import _dump_with_secrets
from agentscope.permission import PermissionMode

from bocomadp.config.app_config import ModelEntry
from bocomadp.deerflow.credentials import (
    DEFAULT_CREDENTIAL_OWNER,
    default_credential_id,
    ensure_default_credentials,
    user_credential_id,
)
from bocomadp.deerflow.routers.deerflow_chat import (
    CreateRunRequest,
    _prepare_session_for_run,
    _resolve_chat_model_config,
    _resolve_requested_model_name,
)
USER_ID = "u1"
AGENT_ID = "lead_agent"


# ── 测试数据构造 ──────────────────────────────────────────────────────


def _make_model_entry(
    provider_id: str = "ds",
    model_name: str = "deepseek-chat",
    api_key: str = "sk-ds",
    is_active: bool = True,
    **overrides: Any,
) -> ModelEntry:
    kwargs: dict[str, Any] = {
        "provider_id": provider_id,
        "display_name": f"DS {provider_id}",
        "provider_type": "deepseek",
        "model_name": model_name,
        "api_key": api_key,
        "base_url": "https://api.deepseek.com",
        "is_active": is_active,
        "supports_thinking": True,
    }
    kwargs.update(overrides)
    return ModelEntry(**kwargs)


def _credential_data(
    credential_id: str,
    api_key: str = "sk-x",
    base_url: str = "https://api.deepseek.com",
) -> dict[str, Any]:
    """手写 DeepSeek credential dump（含 type 判别字段）。"""
    return {
        "type": "deepseek_credential",
        "id": credential_id,
        "name": "",
        "api_key": api_key,
        "base_url": base_url,
    }


class FakeCredential:
    """storage 中的 credential 记录最小实现。"""

    def __init__(self, credential_id: str, user_id: str, data: dict) -> None:
        self.id = credential_id
        self.user_id = user_id
        self.data = data


class FakeStorage:
    """get/upsert credential 与 session 的内存实现（带失败注入）。"""

    def __init__(self) -> None:
        self.credentials: dict[tuple[str, str], FakeCredential] = {}
        self.sessions: dict[tuple[str, str, str], Any] = {}
        self.upsert_counts: dict[tuple[str, str], int] = {}
        # id 含该 marker 的 credential upsert 时抛错（失败不阻断测试用）
        self.fail_marker: str | None = None

    def seed_credential(
        self,
        user_id: str,
        credential_id: str,
        api_key: str = "sk-x",
    ) -> None:
        self.credentials[(user_id, credential_id)] = FakeCredential(
            credential_id,
            user_id,
            _credential_data(credential_id, api_key=api_key),
        )

    async def get_credential(self, user_id: str, credential_id: str):
        return self.credentials.get((user_id, credential_id))

    async def list_credentials(self, user_id: str):
        return [
            record
            for (owner, _), record in self.credentials.items()
            if owner == user_id
        ]

    async def upsert_credential(self, user_id: str, credential) -> None:
        if self.fail_marker and self.fail_marker in credential.id:
            raise RuntimeError(f"injected failure for {credential.id}")
        key = (user_id, credential.id)
        self.upsert_counts[key] = self.upsert_counts.get(key, 0) + 1
        self.credentials[key] = FakeCredential(
            credential.id,
            user_id,
            _dump_with_secrets(credential)
            if hasattr(credential, "model_dump")
            else dict(credential.data),
        )

    async def get_session(self, user_id: str, agent_id: str, session_id: str):
        return self.sessions.get((user_id, agent_id, session_id))

    async def upsert_session(
        self,
        user_id,
        agent_id,
        config,
        session_id,
        state=None,
    ) -> None:
        self.sessions[(user_id, agent_id, session_id)] = SimpleNamespace(
            config=config,
            state=state,
        )

    async def get_agent(self, user_id: str, agent_id: str):
        return None

    async def upsert_agent(self, user_id: str, record) -> None:
        return None


class FakeActiveModel:
    def __init__(self, provider_id: str, model_name: str) -> None:
        self.provider_id = provider_id
        self.model_name = model_name


class FakeRequest:
    """最小 Request 替身：app.state.provider_manager.get_active_model。"""

    def __init__(self, active: FakeActiveModel | None = None) -> None:
        manager = SimpleNamespace(get_active_model=lambda: active)
        self.app = SimpleNamespace(state=SimpleNamespace(provider_manager=manager))


class FakeWorkspaceManager:
    def assign_workspace_id(self, **kwargs) -> str:
        return "ws-test"


# ── _resolve_requested_model_name ─────────────────────────────────────


def test_resolve_requested_model_name_from_custom_params() -> None:
    """唯一通道 custom_params.llm_model_name；SDK 字段 context / config 忽略。"""
    body = CreateRunRequest(
        context={"model_name": "from-context"},
        config={"configurable": {"model_name": "from-config"}},
        custom_params={"llm_model_name": "from-custom"},
    )
    assert _resolve_requested_model_name(body.custom_params) == "from-custom"


def test_resolve_requested_model_name_empty_fallback() -> None:
    """缺失返回空串；空白值视为缺失；其他 SDK 字段不参与解析。"""
    assert _resolve_requested_model_name(None) == ""
    body = CreateRunRequest(
        context={"model_name": "from-context"},
        config={"configurable": {"model_name": "from-config"}},
        custom_params={"llm_model_name": "  "},
    )
    assert _resolve_requested_model_name(body.custom_params) == ""


# ── ensure_default_credentials ────────────────────────────────────────


def test_ensure_default_credentials_upserts_under_default_user(
    monkeypatch,
) -> None:
    """每个条目以 deerflow-default-<provider_id> 归属 default 用户入库。"""
    entries = [_make_model_entry("ds"), _make_model_entry("ds-r1", "r1")]
    monkeypatch.setattr(
        "bocomadp.deerflow.credentials.load_model_entries",
        lambda: entries,
    )
    storage = FakeStorage()

    import asyncio

    asyncio.run(ensure_default_credentials(storage))

    for entry in entries:
        record = storage.credentials[
            (DEFAULT_CREDENTIAL_OWNER, default_credential_id(entry.provider_id))
        ]
        assert record is not None
        assert record.id == default_credential_id(entry.provider_id)
    assert len(storage.credentials) == 2


def test_ensure_default_credentials_idempotent(monkeypatch) -> None:
    """重复调用走 upsert：记录不增、最新参数覆盖。"""
    entry = _make_model_entry("ds", api_key="sk-v2")
    monkeypatch.setattr(
        "bocomadp.deerflow.credentials.load_model_entries",
        lambda: [entry],
    )
    storage = FakeStorage()

    import asyncio

    asyncio.run(ensure_default_credentials(storage))
    asyncio.run(ensure_default_credentials(storage))

    key = (DEFAULT_CREDENTIAL_OWNER, default_credential_id("ds"))
    assert len(storage.credentials) == 1
    assert storage.upsert_counts[key] == 2


def test_ensure_default_credentials_failure_does_not_block(
    monkeypatch,
) -> None:
    """单条目失败仅跳过该条，其余条目照常入库。"""
    entries = [_make_model_entry("bad"), _make_model_entry("good")]
    monkeypatch.setattr(
        "bocomadp.deerflow.credentials.load_model_entries",
        lambda: entries,
    )
    storage = FakeStorage()
    storage.fail_marker = default_credential_id("bad")

    import asyncio

    asyncio.run(ensure_default_credentials(storage))

    assert (DEFAULT_CREDENTIAL_OWNER, default_credential_id("good")) in (
        storage.credentials
    )
    assert (DEFAULT_CREDENTIAL_OWNER, default_credential_id("bad")) not in (
        storage.credentials
    )


# ── _resolve_chat_model_config ────────────────────────────────────────


def _patch_config_loader(
    monkeypatch,
    models: list[ModelEntry],
) -> None:
    monkeypatch.setattr(
        "bocomadp.deerflow.routers.deerflow_chat.load_model_entries",
        lambda: models,
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_resolve_chat_model_config_by_model_name(monkeypatch) -> None:
    """``model_name`` 为模型名：透传为 config.model；凭证表为空时回退 active
    provider 对应条目创建用户凭证。"""
    entries = [_make_model_entry("ds"), _make_model_entry("ds-r1", "r1")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
            model_name="r1",
        ),
    )

    assert config is not None
    assert config.model == "r1"
    assert config.credential_id == user_credential_id(USER_ID, "ds")
    assert config.type == "deepseek_credential"


def test_resolve_chat_model_config_by_provider_id(monkeypatch) -> None:
    """``model_name`` 非约定凭证 id（provider_id）：原样透传为模型名。"""
    entries = [_make_model_entry("ds"), _make_model_entry("ds-r1", "r1")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
            model_name="ds-r1",
        ),
    )

    assert config is not None
    assert config.model == "ds-r1"
    assert config.credential_id == user_credential_id(USER_ID, "ds")


def test_resolve_chat_model_config_unmatched_falls_back_to_active_provider(
    monkeypatch,
) -> None:
    """``model_name`` 任意值一律透传为模型名；凭证表为空时凭证回退 active
    provider 对应条目创建。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
            model_name="nope",
        ),
    )

    assert config is not None
    assert config.model == "nope"
    assert config.credential_id == user_credential_id(USER_ID, "ds")


def test_resolve_chat_model_config_reuses_user_credential(
    monkeypatch,
) -> None:
    """用户维度凭证已存在：直接引用，不重新 upsert。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()
    own_id = user_credential_id(USER_ID, "ds")
    storage.seed_credential(USER_ID, own_id, api_key="sk-custom")

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
        ),
    )

    assert config is not None
    assert config.credential_id == own_id
    assert (USER_ID, own_id) not in storage.upsert_counts


def test_resolve_chat_model_config_copies_from_default_credential(
    monkeypatch,
) -> None:
    """用户凭证缺失：从 default 凭证复制参数入库后引用。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()
    storage.seed_credential(
        DEFAULT_CREDENTIAL_OWNER,
        default_credential_id("ds"),
        api_key="sk-default",
    )

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
        ),
    )

    own_id = user_credential_id(USER_ID, "ds")
    assert config is not None
    assert config.credential_id == own_id
    assert storage.upsert_counts[(USER_ID, own_id)] == 1
    copied = storage.credentials[(USER_ID, own_id)]
    assert copied.data["api_key"] == "sk-default"


def test_resolve_chat_model_config_falls_back_to_entry_without_default(
    monkeypatch,
) -> None:
    """default 凭证亦缺失：回退 config.yaml 条目参数创建用户凭证。"""
    entries = [_make_model_entry("ds", api_key="sk-entry")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()

    config = _run(
        _resolve_chat_model_config(
            storage,
            FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
        ),
    )

    own_id = user_credential_id(USER_ID, "ds")
    assert config is not None
    assert config.credential_id == own_id
    created = storage.credentials[(USER_ID, own_id)]
    assert created.data["api_key"] == "sk-entry"


def test_resolve_chat_model_config_unknown_entry_returns_none(
    monkeypatch,
) -> None:
    """无 active provider：返回 None 不阻断。"""
    _patch_config_loader(monkeypatch, [])
    config = _run(
        _resolve_chat_model_config(
            FakeStorage(),
            FakeRequest(active=None),
            USER_ID,
        ),
    )
    assert config is None


# ── _prepare_session_for_run ─────────────────────────────────────────


def _run_prepare_session(
    storage: FakeStorage,
    model_name: str = "",
    request: FakeRequest | None = None,
) -> None:
    _run(
        _prepare_session_for_run(
            storage,
            FakeWorkspaceManager(),
            request or FakeRequest(active=FakeActiveModel("ds", "deepseek-chat")),
            USER_ID,
            AGENT_ID,
            "s1",
            model_name=model_name,
        ),
    )


def _session_key() -> tuple[str, str, str]:
    return (USER_ID, AGENT_ID, "s1")


def test_prepare_session_creates_with_backfilled_model_config(
    monkeypatch,
) -> None:
    """首次创建：session 自动补齐 chat_model_config。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()

    _run_prepare_session(storage)

    session = storage.sessions[_session_key()]
    assert session.config.workspace_id == "ws-test"
    assert session.config.chat_model_config.model == "deepseek-chat"


def test_prepare_session_creates_with_default_permission_mode(
    monkeypatch,
) -> None:
    """首次创建：permission_context.mode 取配置项 default_permission_mode。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    monkeypatch.setattr(
        "bocomadp.deerflow.routers.deerflow_chat.get_app_config",
        lambda: SimpleNamespace(
            default_permission_mode=PermissionMode.BYPASS,
        ),
    )
    storage = FakeStorage()

    _run_prepare_session(storage)

    session = storage.sessions[_session_key()]
    assert session.state.permission_context.mode == PermissionMode.BYPASS


def test_prepare_session_preserves_state_when_race_created(
    monkeypatch,
) -> None:
    """竞态防护：upsert 前会话已被并发请求建好时不再覆盖。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    monkeypatch.setattr(
        "bocomadp.deerflow.routers.deerflow_chat.get_app_config",
        lambda: SimpleNamespace(
            default_permission_mode=PermissionMode.BYPASS,
        ),
    )
    storage = FakeStorage()
    # 模拟并发请求：_prepare_session_for_run 首次 get_session 返回 None，
    # 解析模型配置期间另一请求已建好会话。
    existing = SimpleNamespace(
        config=SessionConfig(workspace_id="ws-other"),
    )
    created = False
    original_get = storage.get_session

    async def racy_get(user_id: str, agent_id: str, session_id: str):
        nonlocal created
        result = await original_get(user_id, agent_id, session_id)
        if result is None and not created:
            storage.sessions[_session_key()] = existing
            created = True
        return result

    storage.get_session = racy_get

    _run_prepare_session(storage)

    assert storage.sessions[_session_key()] is existing



def test_prepare_session_updates_config_on_model_switch(
    monkeypatch,
) -> None:
    """模型切换（三元组不一致）：已有 session 的 config 被更新。"""
    entries = [_make_model_entry("ds"), _make_model_entry("ds-r1", "r1")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()
    storage.sessions[_session_key()] = SimpleNamespace(
        config=SessionConfig(
            workspace_id="ws1",
            chat_model_config=ChatModelConfig(
                type="deepseek",
                credential_id=user_credential_id(USER_ID, "ds"),
                model="deepseek-chat",
                parameters={},
            ),
        ),
    )

    _run_prepare_session(storage, model_name="r1")

    session = storage.sessions[_session_key()]
    assert session.config.chat_model_config.model == "r1"
    assert session.config.chat_model_config.credential_id == user_credential_id(
        USER_ID,
        "ds",
    )


def test_prepare_session_no_update_when_config_matches(monkeypatch) -> None:
    """一致三元组（HITL 续跑同 thread 模型名）：不触发更新。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()
    storage.sessions[_session_key()] = SimpleNamespace(
        config=SessionConfig(
            workspace_id="ws1",
            chat_model_config=ChatModelConfig(
                type="deepseek",
                credential_id=user_credential_id(USER_ID, "ds"),
                model="deepseek-chat",
                parameters={},
            ),
        ),
    )
    # 记录原始 config 引用，upsert 后 sessions 字典会替换条目
    before = storage.sessions[_session_key()]

    _run_prepare_session(storage)

    assert storage.sessions[_session_key()] is before


def test_prepare_session_backfills_missing_model_config(
    monkeypatch,
) -> None:
    """已有 session 但 chat_model_config 为空：backfill。"""
    entries = [_make_model_entry("ds")]
    _patch_config_loader(monkeypatch, entries)
    storage = FakeStorage()
    storage.sessions[_session_key()] = SimpleNamespace(
        config=SessionConfig(workspace_id="ws1"),
    )

    _run_prepare_session(storage)

    session = storage.sessions[_session_key()]
    assert session.config.chat_model_config.model == "deepseek-chat"
