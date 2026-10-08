# -*- coding: utf-8 -*-
"""read_uploaded_file / list_uploaded_files 会话身份注入测试。

核心回归点：模型只传 virtual_path（不带 user_id / session_id）时，工具
从 build_agent_tools 设置的 ContextVar 读取当前会话身份——便签有值则
正常查库，便签为空才提示补充；显式传参始终优先于便签。

背景：这两个工具此前没有 view_image_tool 同款的 ContextVar 兜底，
而"框架自动注入"机制并不存在，导致真机上工具报
"缺少 user_id / session_id"、agent 只能按大纲回答。
"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from bocomadp.tools.agent_factory_tools import (
    _current_session_id,
    _current_user_id,
)
from bocomadp.tools.builtin_tools import (
    list_uploaded_files,
    read_uploaded_file,
)


def _make_record() -> MagicMock:
    """构造一条上传记录桩：已转文本的 Markdown 文件。"""
    rec = MagicMock()
    rec.is_image = False
    rec.converted = True
    rec.convert_format = "md"
    rec.original_name = "report.md"
    rec.virtual_path = "/workspace/user-data/uploads/report.md"
    rec.markdown = "# hello md"
    return rec


def _make_fake_db(record: MagicMock | None, rows: list | None = None) -> MagicMock:
    db = MagicMock()
    db.get_by_session_file = MagicMock(return_value=record)
    db.list_by_session = MagicMock(return_value=rows if rows is not None else [])
    return db


class _IdentityContext:
    """临时设置两个身份 ContextVar，用例结束恢复原值。"""

    def __init__(self, user_id: str, session_id: str) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._tokens: list = []

    def __enter__(self) -> "_IdentityContext":
        self._tokens = [
            _current_user_id.set(self._user_id),
            _current_session_id.set(self._session_id),
        ]
        return self

    def __exit__(self, *exc: object) -> None:
        _current_user_id.reset(self._tokens[0])
        _current_session_id.reset(self._tokens[1])


class TestIdentityFallback(IsolatedAsyncioTestCase):
    async def test_contextvar_fallback_used(self) -> None:
        """空参调用 + 便签有值 → 从 ContextVar 取身份正常查库，不再报缺。"""
        with _IdentityContext(user_id="u9", session_id="s9"):
            with patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(
                    None, rows=[_make_record()],
                ),
            ) as db:
                result = list_uploaded_files()
                assert "缺少 user_id / session_id" not in result
                assert "report.md" in result
                db.return_value.list_by_session.assert_called_once_with(
                    "u9", "", "s9",
                )

    async def test_read_uses_contextvar_identity(self) -> None:
        """read_uploaded_file 同款兜底：便签身份送达查库参数。"""
        with _IdentityContext(user_id="u9", session_id="s9"):
            with patch(
                "bocomadp.uploads.manager.resolve_upload_parts",
                return_value=("", "", "report.md"),
            ):
                with patch(
                    "bocomadp.uploads.db.get_uploads_db",
                    return_value=_make_fake_db(_make_record()),
                ) as db:
                    result = read_uploaded_file(
                        virtual_path="/workspace/user-data/uploads/report.md",
                    )
        assert "hello md" in result
        db.return_value.get_by_session_file.assert_called_once_with(
            "u9", "s9", "report.md", "",
        )

    async def test_explicit_params_win(self) -> None:
        """显式传参优先：与便签值不同时以显式参数为准。"""
        with _IdentityContext(user_id="u9", session_id="s9"):
            with patch(
                "bocomadp.uploads.manager.resolve_upload_parts",
                return_value=("", "", "report.md"),
            ):
                with patch(
                    "bocomadp.uploads.db.get_uploads_db",
                    return_value=_make_fake_db(_make_record()),
                ) as db:
                    read_uploaded_file(
                        virtual_path="/workspace/user-data/uploads/report.md",
                        user_id="u1",
                        session_id="s1",
                    )
        db.return_value.get_by_session_file.assert_called_once_with(
            "u1", "s1", "report.md", "",
        )

    async def test_empty_contextvar_still_prompts(self) -> None:
        """便签为空（会话 id 缺省）→ 仍返回提示，不触库。"""
        with _IdentityContext(user_id="default", session_id=""):
            with patch(
                "bocomadp.uploads.db.get_uploads_db",
                return_value=_make_fake_db(None),
            ) as db:
                list_result = list_uploaded_files()
                read_result = read_uploaded_file(
                    virtual_path="/workspace/user-data/uploads/report.md",
                )
        assert "缺少 user_id / session_id" in list_result
        assert "缺少 user_id / session_id" in read_result
        db.return_value.list_by_session.assert_not_called()
        db.return_value.get_by_session_file.assert_not_called()
