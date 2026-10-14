# -*- coding: utf-8 -*-
"""同会话同名文件自动改名测试（``A.docx`` → ``A(1).docx``）。

覆盖 _dedupe_session_filename 的占用判定组合：元数据库 / 磁盘文件 /
双占用递增 / 无扩展名 / 跨会话同名互不干扰。
"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from bocomadp.routers.uploads import _dedupe_session_filename


class _FakeBackend:
    """工作区 backend 桩：维护一个已存在路径集合。"""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()

    def join_path(self, workdir: str, name: str) -> str:
        return f"{workdir}/{name}"

    async def file_exists(self, path: str) -> bool:
        return path in self.existing


def _make_db(taken: set[str], session_id: str = "s1") -> MagicMock:
    """uploads DB 桩：仅 session_id 会话下 taken 集合内的名字视为占用。"""
    db = MagicMock()
    db.get_by_session_file = MagicMock(
        side_effect=lambda user, session, name: (
            MagicMock() if session == session_id and name in taken else None
        ),
    )
    return db


def _patch_db(db: MagicMock):
    return patch("bocomadp.routers.uploads.get_uploads_db", return_value=db)


class TestDedupeSessionFilename(IsolatedAsyncioTestCase):
    async def test_fresh_name_unchanged(self) -> None:
        """库和盘都没占用 → 名字原样返回。"""
        with _patch_db(_make_db(set())):
            result = await _dedupe_session_filename(
                backend=_FakeBackend(),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="A.docx",
            )
        assert result == "A.docx"

    async def test_db_taken_renamed(self) -> None:
        """库里已有同名记录 → 改为 A(1).docx。"""
        with _patch_db(_make_db({"A.docx"})):
            result = await _dedupe_session_filename(
                backend=_FakeBackend(),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="A.docx",
            )
        assert result == "A(1).docx"

    async def test_disk_taken_renamed(self) -> None:
        """库没有但磁盘已有同名文件（历史残留）→ 同样改名。"""
        with _patch_db(_make_db(set())):
            result = await _dedupe_session_filename(
                backend=_FakeBackend({"/w/user-data/uploads/A.docx"}),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="A.docx",
            )
        assert result == "A(1).docx"

    async def test_sequence_increments(self) -> None:
        """A 与 A(1) 都占用 → 递增到 A(2)。"""
        with _patch_db(_make_db({"A.docx", "A(1).docx"})):
            result = await _dedupe_session_filename(
                backend=_FakeBackend(),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="A.docx",
            )
        assert result == "A(2).docx"

    async def test_no_extension(self) -> None:
        """无扩展名文件 → README(1)。"""
        with _patch_db(_make_db({"README"})):
            result = await _dedupe_session_filename(
                backend=_FakeBackend(),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="README",
            )
        assert result == "README(1)"

    async def test_other_session_no_conflict(self) -> None:
        """同名记录属于其他会话 → 不占用（跨会话由 workdir 物理隔离）。"""
        with _patch_db(_make_db({"A.docx"}, session_id="s-other")):
            result = await _dedupe_session_filename(
                backend=_FakeBackend(),
                workdir="/w",
                user_id="u1",
                session_id="s1",
                stored_name="A.docx",
            )
        assert result == "A.docx"
