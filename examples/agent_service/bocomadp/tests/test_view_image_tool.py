# -*- coding: utf-8 -*-
"""view_image_tool 图片解析工具测试（mock 视觉模型 / 上传记录 / 配置）。

覆盖两条链：
1. 工具行为（TestViewImageTool）：路径校验 → 记录定位 → 图片校验 →
   模型获取 → 分析结果与连接池释放（aclose）。
2. 模型决策（TestGetVisionModel）：PG runtime_configs ``view_image``
   配置为唯一来源，无记录/未启用/凭证缺失/构建失败均返回 None。
"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from bocomadp.config.app_config import ImageParseConfig
from bocomadp.credential import ELLMCredential
from bocomadp.tools import builtin_tools
from bocomadp.tools.builtin_tools import (
    _get_vision_model,
    set_tool_runtime_deps,
    view_image_tool,
)
from bocomadp.uploads.manager import UploadError

_CREDENTIAL = ELLMCredential(
    api_key="test-key",
    base_url="http://localhost",
    model=None,  # 凭证不绑定单模型（新逻辑）
    scene_code="P2024146",
    api_key_url="http://localhost/createSceneApiKey.do",
)


class _FakeEllmChatModel:
    """EllmChatModel 桩（_get_vision_model 内 isinstance 检查用）。"""


class _FakeVisionModel(_FakeEllmChatModel):
    """模拟视觉模型实例：记录 set_api_key 与 aclose 调用。"""

    def __init__(self) -> None:
        self.api_key: str | None = None
        self.closed = False

    def set_api_key(self, key: str) -> None:
        self.api_key = key

    def set_refresh_key_callback(self, cb) -> None:  # noqa: ANN001
        self.refresh_cb = cb

    def set_auth_invalidate_callback(self, cb) -> None:  # noqa: ANN001
        self.invalidate_cb = cb

    async def aclose(self) -> None:
        self.closed = True


class _NoCloseModel:
    """无 aclose 方法的模型（回退路径可能返回非 EllmChatModel）。"""


def _make_record(is_image: bool = True) -> MagicMock:
    rec = MagicMock()
    rec.is_image = is_image
    rec.mime_type = "image/png"
    rec.base64 = "aGVsbG8="
    rec.original_name = "photo.png"
    return rec


def _make_fake_db(record: MagicMock | None) -> MagicMock:
    db = MagicMock()
    db.get_by_session_file = MagicMock(return_value=record)
    return db


class TestViewImageTool(IsolatedAsyncioTestCase):
    async def test_missing_user_or_session(self) -> None:
        """缺 user_id / session_id（ContextVar 兜底仍为空）→ 提示。"""
        result = await view_image_tool(
            virtual_path="/workspace/user-data/uploads/photo.png",
        )
        assert "缺少 user_id / session_id" in result

    async def test_invalid_virtual_path(self) -> None:
        """虚拟路径非法 → 路径解析失败（不触库、不触模型）。"""
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            side_effect=UploadError("bad path"),
        ):
            result = await view_image_tool(
                virtual_path="bad",
                user_id="u1",
                session_id="s1",
            )
        assert "路径解析失败" in result

    async def test_record_not_found(self) -> None:
        """上传记录不存在 → 提示，不触模型。"""
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            return_value=("", "", "photo.png"),
        ):
            with mock.patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(None),
            ):
                result = await view_image_tool(
                    virtual_path="/workspace/user-data/uploads/photo.png",
                    user_id="u1",
                    session_id="s1",
                )
        assert "上传记录不存在" in result

    async def test_not_image(self) -> None:
        """非图片文件 → 拒绝解析。"""
        rec = _make_record(is_image=False)
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            return_value=("", "", "photo.png"),
        ):
            with mock.patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(rec),
            ):
                with mock.patch.object(
                    builtin_tools,
                    "_get_vision_model",
                    new=AsyncMock(),
                ) as get_model:
                    result = await view_image_tool(
                        virtual_path="/workspace/user-data/uploads/photo.png",
                        user_id="u1",
                        session_id="s1",
                    )
        assert "不是可解析的图片" in result
        get_model.assert_not_awaited()

    async def test_no_vision_model(self) -> None:
        """无统一多模态模型（未配置）→ 提示经接口配置。"""
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            return_value=("", "", "photo.png"),
        ):
            with mock.patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(_make_record()),
            ):
                with mock.patch.object(
                    builtin_tools,
                    "_get_vision_model",
                    new=AsyncMock(return_value=None),
                ):
                    result = await view_image_tool(
                        virtual_path="/workspace/user-data/uploads/photo.png",
                        user_id="u1",
                        session_id="s1",
                    )
        assert "未找到可用的多模态模型" in result
        assert "/api/config/view_image" in result
        assert "config.yaml" not in result  # 不再提示 yaml 配置

    async def test_success_closes_model(self) -> None:
        """成功路径：返回分析结果，且用后 aclose 释放连接池。"""
        model = _FakeVisionModel()
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            return_value=("", "", "photo.png"),
        ):
            with mock.patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(_make_record()),
            ):
                with mock.patch.object(
                    builtin_tools,
                    "_get_vision_model",
                    new=AsyncMock(return_value=model),
                ):
                    result = await view_image_tool(
                        virtual_path="/workspace/user-data/uploads/photo.png",
                        user_id="u1",
                        session_id="s1",
                    )
        assert "图片分析结果 (photo.png)" in result
        assert model.closed is True

    async def test_success_model_without_aclose(self) -> None:
        """回退路径模型可能无 aclose：不应抛异常。"""
        model = _NoCloseModel()
        with mock.patch(
            "bocomadp.uploads.manager.resolve_upload_parts",
            return_value=("", "", "photo.png"),
        ):
            with mock.patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(_make_record()),
            ):
                with mock.patch.object(
                    builtin_tools,
                    "_get_vision_model",
                    new=AsyncMock(return_value=model),
                ):
                    result = await view_image_tool(
                        virtual_path="/workspace/user-data/uploads/photo.png",
                        user_id="u1",
                        session_id="s1",
                    )
        assert "图片分析结果 (photo.png)" in result


def _patch_get_config(cfg: ImageParseConfig | None):
    """patch _get_vision_model 内 get_typed_config 返回指定配置。"""
    return mock.patch(
        "bocomadp.runtime_config_store.get_typed_config",
        new=AsyncMock(return_value=cfg),
    )


def _make_cfg(enabled: bool = True) -> ImageParseConfig:
    return ImageParseConfig(
        enabled=enabled,
        user_id="u1",
        credential_id="c1",
        model_name="Qwen3-VL-30B-A3B-Instruct",
    )


class _FakeRefresher:
    """EllmKeyRefresher 桩：记录构造参数并返回固定 key。"""

    def __init__(self, storage, message_bus, user_id) -> None:  # noqa: ANN001
        self.storage = storage
        self.message_bus = message_bus
        self.user_id = user_id

    async def ensure_fresh_key(self, credential_id: str):
        return "fresh-key", None

    async def force_refresh_key(self, credential_id: str) -> str:
        return "force-key"

    async def invalidate_key(self, credential_id: str) -> None:
        return None


class TestGetVisionModel(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_storage = builtin_tools._tool_storage
        self._orig_bus = builtin_tools._tool_message_bus

    def tearDown(self) -> None:
        builtin_tools._tool_storage = self._orig_storage
        builtin_tools._tool_message_bus = self._orig_bus

    async def test_no_config_returns_none(self) -> None:
        """PG 无记录 → 返回 None（不回退 config.yaml）。"""
        with _patch_get_config(None):
            model = await _get_vision_model()
        assert model is None

    async def test_disabled_returns_none(self) -> None:
        """PG 未启用 → 返回 None，不查凭证。"""
        storage = MagicMock()
        set_tool_runtime_deps(storage, MagicMock())
        with _patch_get_config(_make_cfg(enabled=False)):
            model = await _get_vision_model()
        assert model is None
        storage.get_credential.assert_not_called()

    async def test_enabled_builds_and_injects_key(self) -> None:
        """PG 启用 + 凭证存在 → 构建统一模型并注入刷新后的 key。"""
        storage = MagicMock()
        record = MagicMock(data=_CREDENTIAL.model_dump())
        storage.get_credential = AsyncMock(return_value=record)
        set_tool_runtime_deps(storage, MagicMock())

        with _patch_get_config(_make_cfg(enabled=True)):
            with mock.patch(
                "bocomadp.providers.ellm_chat_model.EllmChatModel",
                _FakeEllmChatModel,
            ):
                with mock.patch(
                    "bocomadp.view_image_model_builder."
                    "build_image_parse_model",
                    return_value=_FakeVisionModel(),
                ):
                    with mock.patch(
                        "bocomadp.providers.ellm_key.EllmKeyRefresher",
                        _FakeRefresher,
                    ):
                        model = await _get_vision_model()

        assert isinstance(model, _FakeVisionModel)
        assert model.api_key == "fresh-key"  # key 已注入
        storage.get_credential.assert_awaited_once_with("u1", "c1")

    async def test_credential_missing_returns_none(self) -> None:
        """PG 启用但凭证查不到 → 返回 None。"""
        storage = MagicMock()
        storage.get_credential = AsyncMock(return_value=None)
        set_tool_runtime_deps(storage, MagicMock())

        with _patch_get_config(_make_cfg(enabled=True)):
            model = await _get_vision_model()
        assert model is None

    async def test_build_failure_returns_none(self) -> None:
        """统一模型构建异常 → 记日志并返回 None。"""
        storage = MagicMock()
        storage.get_credential = AsyncMock(
            return_value=MagicMock(data=_CREDENTIAL.model_dump()),
        )
        set_tool_runtime_deps(storage, MagicMock())

        with _patch_get_config(_make_cfg(enabled=True)):
            with mock.patch(
                "bocomadp.view_image_model_builder."
                "build_image_parse_model",
                side_effect=RuntimeError("model boom"),
            ):
                model = await _get_vision_model()
        assert model is None

    async def test_deps_not_injected_returns_none(self) -> None:
        """未注入 storage（旧部署）→ 返回 None。"""
        set_tool_runtime_deps(None, None)
        with _patch_get_config(_make_cfg(enabled=True)):
            model = await _get_vision_model()
        assert model is None
