# -*- coding: utf-8 -*-
"""deerflow 模型解析链单测：用户凭证表挑选 + 模型名透传。

Contract under test:

- ``is_deerflow_credential_id`` 识别约定凭证 id 形态
  （``deerflow-<user>-<provider>`` / ``deerflow-default-<provider>``）
  并解析出 provider_id；模型名 / 任意 uuid / 他人凭证 id 返回 None。
- ``_resolve_chat_model_config`` 新契约（一凭证多模型）：
  - ``model_name`` 非空一律视为模型名透传（不再支持凭证 id 直传）；
  - ``list_credentials(user_id)`` 挑选：type 可反序列化过滤、ELLM
    优先、id 稳定排序取第一个；
  - 用户凭证为空 → default 用户凭证同规则挑选并复制入库；
  - 再空 → 回退 config.yaml 条目参数创建（ELLM 条目 api_key 空放行，
    非 ELLM 条目 api_key 空返回 None）。
- 模型名：``model_name`` 直接透传（凭证 model 字段不参与绑定）；缺失时
  回退凭证 model 字段值 → config.yaml 匹配条目 model_name → 全局
  active provider。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from agentscope.app.storage import ChatModelConfig, CredentialRecord

from bocomadp.config.app_config import ModelEntry
from bocomadp.credential import (  # noqa: F401 — 导入即注册，from_dict 反序列化需要
    ELLMCredential,
)
from bocomadp.deerflow.credentials import is_deerflow_credential_id
from bocomadp.deerflow.routers import deerflow_chat as chat_mod

_ELLM_PROVIDER = "ellm-deepseek"
_DEEPSEEK_PROVIDER = "deepseek"


def _ellm_entry(api_key: str = "") -> ModelEntry:
    """内网 ELLM 模型条目（api_key 留空，key 由刷新机制动态获取）。"""
    return ModelEntry(
        provider_id=_ELLM_PROVIDER,
        provider_type="bocom_ellm",
        model_name="deepseek-v4-flash",
        api_key=api_key,
        base_url="http://ellm.example/v1",
    )


def _deepseek_entry(api_key: str) -> ModelEntry:
    """外网静态 key 供应商条目。"""
    return ModelEntry(
        provider_id=_DEEPSEEK_PROVIDER,
        provider_type="deepseek",
        model_name="deepseek-chat",
        api_key=api_key,
    )


def _ellm_record_data(
    credential_id: str | None = None,
    api_key: str = "stored-key",
) -> dict:
    """ELLM 凭证 payload（凭证接口插入形态，含刷新元数据）。"""
    return {
        "id": credential_id or f"deerflow-default-{_ELLM_PROVIDER}",
        "type": "bocom_ellm_credential",
        "api_key": api_key,
        "base_url": "http://ellm.example/v1",
        "organization": None,
        "scene_code": "P2024146",
        "api_key_url": "http://ellm.example/createSceneApiKey.do",
        "inject_think_tag": False,
        "apikey_expires_at": None,
        "model": "deepseek-v4-flash",
    }


def _deepseek_record_data() -> dict:
    """DeepSeek 默认凭证 payload（lifespan 入库形态）。"""
    return {
        "id": f"deerflow-default-{_DEEPSEEK_PROVIDER}",
        "type": "deepseek_credential",
        "api_key": "sk-default",
        "base_url": "https://api.deepseek.com",
    }


class _FakeStorage:
    """按 (user_id, credential_id) 命名空间的内存凭证存储。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], CredentialRecord] = {}
        self.upserts: list[tuple[str, object]] = []

    def seed(
        self,
        user_id: str,
        credential_id: str,
        data: dict,
    ) -> None:
        self._records[(user_id, credential_id)] = CredentialRecord(
            id=credential_id,
            user_id=user_id,
            data=data,
            updated_at=datetime.now(),
        )

    async def get_credential(
        self,
        user_id: str,
        credential_id: str,
    ) -> CredentialRecord | None:
        return self._records.get((user_id, credential_id))

    async def upsert_credential(
        self,
        user_id: str,
        credential: object,
    ) -> str:
        self.upserts.append((user_id, credential))
        return credential.id

    async def list_credentials(
        self,
        user_id: str,
    ) -> list[CredentialRecord]:
        return [
            record
            for (owner, _), record in self._records.items()
            if owner == user_id
        ]


class _FakeRequest:
    """``app.state.provider_manager`` 可配置的最小 Request stand-in。"""

    def __init__(self, active: SimpleNamespace | None = None) -> None:
        class _State:
            provider_manager = SimpleNamespace(
                get_active_model=lambda: active,
            )

        class _App:
            state = _State()

        self.app = _App()


def _resolve(
    storage: _FakeStorage,
    user_id: str,
    model_name: str = "",
    request: _FakeRequest | None = None,
) -> ChatModelConfig | None:
    return asyncio.run(
        chat_mod._resolve_chat_model_config(
            storage,
            request or _FakeRequest(),
            user_id,
            model_name,
        ),
    )


class TestIsDeerflowCredentialId:
    """约定 credential id 识别（纯函数）。"""

    def test_user_credential_id(self) -> None:
        assert (
            is_deerflow_credential_id(
                f"deerflow-user-1-{_ELLM_PROVIDER}",
                "user-1",
            )
            == _ELLM_PROVIDER
        )

    def test_default_credential_id(self) -> None:
        assert (
            is_deerflow_credential_id(
                f"deerflow-default-{_ELLM_PROVIDER}",
                "user-1",
            )
            == _ELLM_PROVIDER
        )

    def test_default_user_matches_own_prefix(self) -> None:
        """user_id 恰为 default 时，用户前缀分支先行命中。"""
        assert (
            is_deerflow_credential_id(
                f"deerflow-default-{_ELLM_PROVIDER}",
                "default",
            )
            == _ELLM_PROVIDER
        )

    def test_model_name_is_not_credential_id(self) -> None:
        assert is_deerflow_credential_id("deepseek-chat", "user-1") is None

    def test_other_users_credential_id_rejected(self) -> None:
        """他人凭证 id 不解析（用户隔离：不跨用户引用）。"""
        assert (
            is_deerflow_credential_id(
                f"deerflow-user-2-{_ELLM_PROVIDER}",
                "user-1",
            )
            is None
        )

    def test_empty_value(self) -> None:
        assert is_deerflow_credential_id("", "user-1") is None

    def test_missing_provider_segment(self) -> None:
        assert is_deerflow_credential_id("deerflow-user-1-", "user-1") is None


class TestResolveChatModelConfig:
    """``_resolve_chat_model_config`` 的解析优先级与复制语义。"""

    def _patch_loaders(
        self,
        monkeypatch,
        models: list[ModelEntry],
    ) -> None:
        monkeypatch.setattr(chat_mod, "load_models_from_yaml", lambda: models)

    def test_model_name_passed_through_with_default_copy(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 为模型名 → 透传为 config.model；用户凭证表为空 →
        default 凭证挑选复制路径。"""
        storage = _FakeStorage()
        storage.seed(
            "default",
            f"deerflow-default-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("sk-test")],
        )

        config = _resolve(storage, "user-1", model_name="deepseek-chat")

        assert config is not None
        assert config.credential_id == (
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}"
        )
        assert config.type == "deepseek_credential"
        assert config.model == "deepseek-chat"
        assert len(storage.upserts) == 1  # 从 default 复制

    def test_non_credential_model_name_always_passed_through(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 非约定凭证 id（如 provider_id）→ 一律透传为模型名，
        不再做 config.yaml 双键匹配。"""
        storage = _FakeStorage()
        storage.seed(
            "default",
            f"deerflow-default-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("sk-test")],
        )

        config = _resolve(storage, "user-1", model_name=_DEEPSEEK_PROVIDER)

        assert config is not None
        assert config.credential_id == (
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}"
        )
        assert config.type == "deepseek_credential"
        assert config.model == _DEEPSEEK_PROVIDER  # model_name 原样透传

    def test_non_ellm_empty_api_key_returns_none(self, monkeypatch) -> None:
        """非 ELLM 条目 api_key 空 → 仍返回 None，且不产生 upsert。"""
        storage = _FakeStorage()
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("")],
        )

        config = _resolve(storage, "user-1", model_name="deepseek-chat")

        assert config is None
        assert storage.upserts == []

    def test_active_provider_fallback(self, monkeypatch) -> None:
        """``model_name`` 为空时回退全局 active provider。"""
        storage = _FakeStorage()
        storage.seed(
            "default",
            f"deerflow-default-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("sk-test")],
        )

        config = _resolve(
            storage,
            "user-1",
            request=_FakeRequest(
                active=SimpleNamespace(
                    provider_id=_DEEPSEEK_PROVIDER,
                    model_name="deepseek-chat",
                ),
            ),
        )

        assert config is not None
        assert config.credential_id == (
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}"
        )

    def test_no_model_name_no_provider_returns_none(self, monkeypatch) -> None:
        """``model_name`` 空 + 无 active → None（原生 404 兜底）。"""
        storage = _FakeStorage()
        self._patch_loaders(monkeypatch, models=[])

        config = _resolve(storage, "user-1")

        assert config is None
        assert storage.upserts == []

    def test_dynamic_model_name_routes_to_unique_ellm_entry(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 为真实模型 ID + 无 active provider + config.yaml 唯一
        ELLM 条目 → 动态路由，config.model == model_name。"""
        storage = _FakeStorage()
        storage.seed(
            "default",
            f"deerflow-default-{_ELLM_PROVIDER}",
            _ellm_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_ellm_entry(api_key="")],
        )

        config = _resolve(storage, "user-1", model_name="Qwen3-235B-A22B")

        assert config is not None
        assert config.type == "bocom_ellm_credential"
        assert config.model == "Qwen3-235B-A22B"
        assert config.credential_id == f"deerflow-user-1-{_ELLM_PROVIDER}"

    def test_entry_fallback_binds_entry_model_not_model_name(
        self,
        monkeypatch,
    ) -> None:
        """凭证全空 → 条目回退入库；凭证 model 取条目 model_name，
        config.model 仍为 ``model_name`` 透传值（凭证 model 字段不参与绑定）。"""
        storage = _FakeStorage()
        self._patch_loaders(
            monkeypatch,
            models=[_ellm_entry(api_key="")],
        )

        config = _resolve(storage, "user-1", model_name="Qwen3-235B-A22B")

        assert config is not None
        assert config.model == "Qwen3-235B-A22B"
        assert config.type == "bocom_ellm_credential"
        assert len(storage.upserts) == 1
        user_id, credential = storage.upserts[0]
        assert user_id == "user-1"
        assert isinstance(credential, ELLMCredential)
        assert credential.model == "deepseek-v4-flash"  # 条目 model_name

    def test_model_name_passed_through_for_non_ellm_credential(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 为真实模型 ID + 凭证为非 ELLM（deepseek）→
        同样透传，不再丢弃 ``model_name`` 或用静态条目名。"""
        storage = _FakeStorage()
        storage.seed(
            "default",
            f"deerflow-default-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("sk-test")],
        )

        config = _resolve(
            storage,
            "user-1",
            model_name="Qwen3-235B-A22B",
            request=_FakeRequest(
                active=SimpleNamespace(
                    provider_id=_DEEPSEEK_PROVIDER,
                    model_name="deepseek-chat",
                ),
            ),
        )

        assert config is not None
        assert config.type == "deepseek_credential"
        assert config.model == "Qwen3-235B-A22B"  # model_name 透传
        assert config.credential_id == (
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}"
        )

    def test_picks_ellm_first_among_user_credentials(
        self,
        monkeypatch,
    ) -> None:
        """用户多凭证：ELLM 优先于其他可反序列化类型，直接引用不复制。"""
        storage = _FakeStorage()
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_ELLM_PROVIDER}",
            _ellm_record_data(
                credential_id=f"deerflow-user-1-{_ELLM_PROVIDER}",
            ),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_ellm_entry(api_key=""), _deepseek_entry("sk-test")],
        )

        config = _resolve(storage, "user-1")

        assert config is not None
        assert config.credential_id == f"deerflow-user-1-{_ELLM_PROVIDER}"
        assert config.type == "bocom_ellm_credential"
        assert storage.upserts == []
        # 模型名回退凭证 model 字段值
        assert config.model == "deepseek-v4-flash"

    def test_model_name_overrides_credential_bound_model(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 模型名透传进 config.model；凭证 model 字段值不被覆盖。"""
        storage = _FakeStorage()
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_ELLM_PROVIDER}",
            _ellm_record_data(
                credential_id=f"deerflow-user-1-{_ELLM_PROVIDER}",
            ),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_ellm_entry(api_key="")],
        )

        config = _resolve(storage, "user-1", model_name="Qwen3-235B-A22B")

        assert config is not None
        assert config.credential_id == f"deerflow-user-1-{_ELLM_PROVIDER}"
        assert config.model == "Qwen3-235B-A22B"  # model_name 透传
        # 凭证 model 字段保持原值（不做绑定过滤）
        assert (
            storage._records[
                ("user-1", f"deerflow-user-1-{_ELLM_PROVIDER}")
            ].data["model"]
            == "deepseek-v4-flash"
        )

    def test_model_falls_back_to_credential_then_entry_then_active(
        self,
        monkeypatch,
    ) -> None:
        """``model_name`` 缺失时的模型名回退链：凭证 model 字段 → 条目 model_name
        → active provider。"""
        # 1) 凭证 model 字段有值 → 用凭证 model
        storage = _FakeStorage()
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_ELLM_PROVIDER}",
            _ellm_record_data(
                credential_id=f"deerflow-user-1-{_ELLM_PROVIDER}",
            ),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_ellm_entry(api_key="")],
        )
        config = _resolve(storage, "user-1")
        assert config is not None
        assert config.model == "deepseek-v4-flash"

        # 2) 凭证无 model 字段 → 条目 model_name
        storage = _FakeStorage()
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(
            monkeypatch,
            models=[_deepseek_entry("sk-test")],
        )
        config = _resolve(storage, "user-1")
        assert config is not None
        assert config.model == "deepseek-chat"

        # 3) 凭证无 model 字段 + 无匹配条目 → active provider
        storage = _FakeStorage()
        storage.seed(
            "user-1",
            f"deerflow-user-1-{_DEEPSEEK_PROVIDER}",
            _deepseek_record_data(),
        )
        self._patch_loaders(monkeypatch, models=[])
        config = _resolve(
            storage,
            "user-1",
            request=_FakeRequest(
                active=SimpleNamespace(
                    provider_id=_DEEPSEEK_PROVIDER,
                    model_name="active-model",
                ),
            ),
        )
        assert config is not None
        assert config.model == "active-model"
