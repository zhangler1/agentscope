# -*- coding: utf-8 -*-
"""POST /files/upload 多文件上传的进程内验证（不依赖真实服务）。

把 storage / workspace_manager 两个依赖 override 成内存桩，直接在内存里发
多个同名 ``file`` 字段的请求，验证后端已能从 list[UploadFile] 收齐全部文件、
并返回长度等于上传数的数组。运行：

    cd examples/agent_service
    venv/python -m pytest bocomadp/tests/test_upload_multi_files.py -s -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app.deps import get_storage, get_workspace_manager

from bocomadp import uploads as uploads_pkg
from bocomadp.routers.uploads import uploads_router
from bocomadp.uploads.db import UploadsDB


# ---------------------------------------------------------------------------
# 内存桩：替代真实的 storage / workspace_manager / backend
# ---------------------------------------------------------------------------
class _FakeConfig:
    workspace_id = "ws-test"


class _FakeSession:
    config = _FakeConfig()


class _FakeBackend:
    """只实现落盘所需的两个方法（join_path / write_file）。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def join_path(self, *parts: str) -> str:
        return os.path.join(*parts)

    async def write_file(self, path: str, data: bytes | str) -> None:
        # join_path 已拼出绝对落盘路径（workdir + 相对子路径），直接写即可
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


class _FakeWorkspace:
    def __init__(self, workdir: str, backend: _FakeBackend) -> None:
        self.workdir = workdir
        self._backend = backend
        self.workspace_id = "ws-test"

    def get_backend(self) -> _FakeBackend:
        return self._backend


class _FakeStorage:
    async def get_session(self, user_id: str, agent_id: str, session_id: str):
        return _FakeSession()


class _FakeWorkspaceManager:
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend

    async def get_workspace(self, user_id, agent_id, session_id, workspace_id):
        return _FakeWorkspace(workdir=str(Path(os.getcwd()) / "ws_test_upload"), backend=self._backend)


HEADERS = {"X-User-ID": "admin"}


@pytest.fixture
def client(tmp_path):
    # uploads DB 用临时库，避免污染真实 BASE_DIR/data/uploads.db
    uploads_pkg.db._uploads_db = UploadsDB(tmp_path / "uploads.db")

    backend = _FakeBackend(tmp_path / "blob")
    app = FastAPI()
    app.dependency_overrides[get_storage] = lambda: _FakeStorage()
    app.dependency_overrides[get_workspace_manager] = lambda: _FakeWorkspaceManager(backend)
    app.include_router(uploads_router)
    with TestClient(app) as c:
        yield c


def _multi_part_files():
    return [
        ("file", ("你好.doc", "你好，这是第一个文档。".encode("utf-8"), "application/msword")),
        ("file", ("怎么啦.doc", "第二个文档内容。".encode("utf-8"), "application/msword")),
        ("file", ("下载.png", b"\x89PNG\r\n\x1a\ndummy", "image/png")),
    ]


def test_upload_multiple_files_returns_array(client):
    resp = client.post(
        "/files/upload",
        files=_multi_part_files(),
        data={"session_id": "sess-001", "agent_id": "_agent-creator"},
        headers=HEADERS,
    )
    print("\n===== 响应状态码 =====")
    print(resp.status_code)
    print("===== 响应体（JSON）=====")
    try:
        body = resp.json()
        print(__import__("json").dumps(body, ensure_ascii=False, indent=2))
    except Exception:
        print(resp.text)

    assert resp.status_code == 200
    arr = resp.json()
    assert isinstance(arr, list)
    assert len(arr) == 3, f"期望 3 个对象，实际 {len(arr)}"
    names = [item["original_name"] for item in arr]
    assert "你好.doc" in names
    assert "怎么啦.doc" in names
    assert "下载.png" in names
    # 第三个是图片，应固化为 base64（is_image）
    png_item = next(i for i in arr if i["original_name"] == "下载.png")
    assert png_item["base64"], "图片应固化为 base64"
    print("\n[OK] 多文件上传返回 3 个对象，且图片已固化 base64")
