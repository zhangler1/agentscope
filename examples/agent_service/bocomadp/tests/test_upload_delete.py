# -*- coding: utf-8 -*-
"""``DELETE /files/upload`` 的物理删除行为。

回归背景：该端点曾误调 backend 上不存在的 ``remove_file``，异常被
``except Exception: pass`` 吞掉，表现为"返回 204、元数据已删，但
workdir/PVC 上文件仍在"。正确接口是 ``delete_path``（``rm -rf`` 语义，
路径不存在时静默 no-op）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from bocomadp.routers import uploads as uploads_router_module


class _FakeBackend:
    """记录删除调用；可选让删除抛错以验证 fail-open。"""

    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self._fail = fail

    def join_path(self, workdir: str, name: str) -> str:
        return f"{workdir}/{name}"

    async def delete_path(self, path: str) -> None:
        if self._fail:
            raise RuntimeError("sandbox unavailable")
        self.deleted.append(path)


def _install(monkeypatch, backend, workdir: str = "/w") -> mock.MagicMock:
    """替换 uploads DB 与 workspace 解析，返回 DB 桩。"""
    db = mock.MagicMock()
    db.get_by_session_file.return_value = SimpleNamespace(stored_name="A.docx")

    workspace = SimpleNamespace(
        get_backend=lambda: backend,
        workdir=workdir,
    )

    async def _resolve(*args, **kwargs):
        return workspace

    monkeypatch.setattr(uploads_router_module, "get_uploads_db", lambda: db)
    monkeypatch.setattr(uploads_router_module, "_resolve_workspace", _resolve)
    return db


def _delete(filename: str = "A.docx") -> None:
    asyncio.run(
        uploads_router_module.delete_upload(
            agent_id="a1",
            session_id="s1",
            filename=filename,
            user_id="u1",
            storage=mock.MagicMock(),
            workspace_manager=mock.MagicMock(),
        ),
    )


def test_delete_removes_original_and_converted_md(monkeypatch):
    """原始文件与同名 .md 都要经 ``delete_path`` 真正删除。"""
    backend = _FakeBackend()
    db = _install(monkeypatch, backend)

    _delete("A.docx")

    assert backend.deleted == [
        "/w/user-data/uploads/A.docx",
        "/w/user-data/uploads/A.md",
    ]
    db.delete.assert_called_once_with("u1", "a1", "s1", "A.docx")


def test_delete_without_extension(monkeypatch):
    """无扩展名文件名：``README`` + ``README.md``。"""
    backend = _FakeBackend()
    _install(monkeypatch, backend)

    _delete("README")

    assert backend.deleted == [
        "/w/user-data/uploads/README",
        "/w/user-data/uploads/README.md",
    ]


def test_physical_failure_still_clears_metadata(monkeypatch):
    """backend 删除失败不阻断元数据清理，但不得抛出。"""
    backend = _FakeBackend(fail=True)
    db = _install(monkeypatch, backend)

    _delete("A.docx")

    assert backend.deleted == []
    db.delete.assert_called_once_with("u1", "a1", "s1", "A.docx")


def test_real_backend_removes_files_on_disk(monkeypatch, tmp_path):
    """真实 ``LocalBackend`` + 真实磁盘文件：删除后文件确实不存在。

    与上面的桩测试互补——桩只验证"调用了哪个方法"，这里验证该调用
    真的把文件从文件系统（与 PVC 同构的 workdir 布局）移除。
    """
    from agentscope.tool import LocalBackend

    upload_dir = tmp_path / "user-data" / "uploads"
    upload_dir.mkdir(parents=True)
    original = upload_dir / "A.docx"
    converted = upload_dir / "A.md"
    original.write_bytes(b"docx-bytes")
    converted.write_text("converted", encoding="utf-8")

    db = _install(monkeypatch, LocalBackend(), workdir=str(tmp_path))

    _delete("A.docx")

    assert not original.exists()
    assert not converted.exists()
    db.delete.assert_called_once_with("u1", "a1", "s1", "A.docx")
