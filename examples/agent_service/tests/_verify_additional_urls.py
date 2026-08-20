# -*- coding: utf-8 -*-
"""临时验证脚本（unittest 版，容器内无 pytest 时替代）。

与 tests/test_deerflow_additional_urls.py 覆盖同组场景，仅用
unittest.mock.patch 替代 pytest monkeypatch。验证通过后删除本文件。

运行（容器内，/tmp/bocomadp_verify 为修改后包副本）：
    cd /tmp && PYTHONPATH=/tmp/bocomadp_verify:/app \
        /app/.venv/bin/python _verify_additional_urls.py
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "/tmp")  # 本脚本所在目录（如容器内）

from bocomadp.deerflow.routers.deerflow_chat import (  # noqa: E402
    CreateRunRequest,
    _download_additional_urls,
)
from bocomadp.routers.uploads import (  # noqa: E402
    _filename_from_url,
    download_urls_to_session,
)
from bocomadp.uploads.db import UploadsDB  # noqa: E402

# PNG 魔数（1x1 透明像素），用于验证图片 base64 固化分支
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082",
)


class FakeBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def join_path(self, workdir: str, rel: str) -> str:
        return f"{workdir}/{rel}"

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]


class FakeWorkspace:
    workdir = "/ws"

    def __init__(self) -> None:
        self.backend = FakeBackend()

    def get_backend(self) -> FakeBackend:
        return self.backend


class FakeSessionRecord:
    class Config:
        workspace_id = "ws-1"

    config = Config()


class FakeStorage:
    async def get_session(self, user_id, agent_id, session_id):
        return FakeSessionRecord()


class FakeWorkspaceManager:
    def __init__(self) -> None:
        self.workspace = FakeWorkspace()

    async def get_workspace(self, user_id, agent_id, session_id, workspace_id):
        return self.workspace


class FakeResponse:
    def __init__(self, content: bytes, content_type: str | None = None) -> None:
        self.content = content
        self.headers = {"content-type": content_type} if content_type else {}

    def raise_for_status(self) -> None:
        pass


class FakeAsyncClient:
    """URL 前缀 ``!`` 抛 RequestError；含 ``?<status>`` 返回对应 HTTP 错误。"""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def get(self, url: str) -> FakeResponse:
        import httpx

        if url.startswith("!"):
            raise httpx.RequestError("boom", request=httpx.Request("GET", url))
        status = 200
        if "?" in url:
            url, status = url.split("?", 1)
            status = int(status)
        resp = self.responses.get(url)
        if resp is None or status >= 400:
            raise httpx.HTTPStatusError(
                "HTTP error",
                request=httpx.Request("GET", url),
                response=httpx.Response(status, request=httpx.Request("GET", url)),
            )
        return resp


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestFilenameFromUrl(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_filename_from_url("http://x.com/a/test.png"), "test.png")
        self.assertEqual(
            _filename_from_url("https://x.com/path/to/report.pdf"), "report.pdf"
        )

    def test_decodes_and_falls_back(self):
        self.assertEqual(
            _filename_from_url("http://x.com/a/%E6%B5%8B%E8%AF%95.txt"), "测试.txt"
        )
        self.assertEqual(_filename_from_url("http://x.com/"), "downloaded")
        self.assertEqual(_filename_from_url("http://x.com/a/"), "downloaded")
        self.assertEqual(_filename_from_url("http://x.com/a/.."), "downloaded")


class TestDownloadUrls(unittest.TestCase):
    def setUp(self):
        self.db = UploadsDB(db_path=":memory:")
        self._db_patch = patch(
            "bocomadp.routers.uploads.get_uploads_db",
            return_value=self.db,
        )
        self._db_patch.start()
        self.cfg = {
            "enabled": True,
            "max_file_size_mb": 1.0,
            "max_file_size_bytes": 1024 * 1024,
            "max_files_per_session": 50,
        }
        self._cfg_patch = patch(
            "bocomadp.routers.uploads.get_upload_config",
            return_value=type("Cfg", (), self.cfg)(),
        )
        self._cfg_patch.start()
        self.storage = FakeStorage()
        self.wm = FakeWorkspaceManager()
        self.backend = self.wm.workspace.backend

    def tearDown(self):
        self._db_patch.stop()
        self._cfg_patch.stop()

    def _download(self, urls):
        return run(
            download_urls_to_session(
                "u1", "a1", "s1", urls, self.storage, self.wm,
            )
        )

    def _client(self, responses):
        import bocomadp.routers.uploads as uploads_mod

        client = FakeAsyncClient(responses)
        uploads_mod.httpx.AsyncClient = lambda *a, **k: client
        return client

    def test_saves_files_and_records(self):
        self._client(
            {
                "http://oss/a.png": FakeResponse(PNG_BYTES, "image/png"),
                "http://oss/note.txt": FakeResponse(b"hello", "text/plain"),
            }
        )
        saved = self._download(["http://oss/a.png", "http://oss/note.txt"])
        self.assertEqual(len(saved), 2)
        self.assertEqual(self.backend.files["/ws/user-data/uploads/a.png"], PNG_BYTES)
        self.assertEqual(self.backend.files["/ws/user-data/uploads/note.txt"], b"hello")
        records = self.db.list_by_session("u1", "a1", "s1")
        self.assertEqual(len(records), 2)
        img = next(r for r in records if r.stored_name == "a.png")
        self.assertEqual(img.mime_type, "image/png")
        self.assertTrue(img.base64)
        self.assertEqual(img.virtual_path, "/workspace/user-data/uploads/a.png")
        txt = next(r for r in records if r.stored_name == "note.txt")
        self.assertFalse(txt.converted)

    def test_skips_failed_and_oversized(self):
        self.cfg["max_file_size_bytes"] = 10
        self._client(
            {
                "http://oss/ok.txt": FakeResponse(b"ok"),
                "http://oss/404.txt": FakeResponse(b"nope"),
                "http://oss/big.txt": FakeResponse(b"x" * 12),
            }
        )
        saved = self._download(
            [
                "http://oss/404.txt?404",
                "!http://oss/timeout.txt",
                "http://oss/big.txt",
                "http://oss/ok.txt",
            ]
        )
        self.assertEqual([r.stored_name for r in saved], ["ok.txt"])

    def test_respects_file_count_limit(self):
        self.cfg["max_files_per_session"] = 2
        self._client(
            {
                "http://oss/1.txt": FakeResponse(b"1"),
                "http://oss/2.txt": FakeResponse(b"2"),
                "http://oss/3.txt": FakeResponse(b"3"),
            }
        )
        saved = self._download(["http://oss/1.txt", "http://oss/2.txt", "http://oss/3.txt"])
        self.assertEqual([r.stored_name for r in saved], ["1.txt", "2.txt"])

    def test_persist_failure_skips_url(self):
        from bocomadp.routers import uploads as uploads_mod
        from bocomadp.uploads.manager import UploadError

        async def boom(**kwargs):
            raise UploadError("write failed")

        with patch.object(uploads_mod, "_persist_uploaded_bytes", new=boom):
            self._client({"http://oss/a.txt": FakeResponse(b"a")})
            saved = self._download(["http://oss/a.txt"])
        self.assertEqual(saved, [])


class TestDownloadAdditionalUrls(unittest.TestCase):
    def test_downloads_cleaned_urls(self):
        from bocomadp.deerflow.routers import deerflow_chat as chat_mod

        seen = {}

        async def fake_download(user_id, agent_id, session_id, urls, storage, wm):
            seen["urls"] = urls
            return []

        body = CreateRunRequest(
            agent_id="a1",
            session_id="s1",
            custom_params={
                "additional_urls": [
                    " http://oss/a.png ",
                    123,
                    "",
                    "http://oss/b.txt",
                ],
                "lang": "zh",
            },
        )
        with patch.object(chat_mod, "download_urls_to_session", new=fake_download):
            result = run(
                _download_additional_urls(
                    body, "u1", "a1", "s1", FakeStorage(), FakeWorkspaceManager(),
                )
            )
        # URL 清洗 + 仅副作用：返回 None（custom_params 含 additional_urls
        # 整体由 _resolve_custom_params 落盘，便于查看历史传参）
        self.assertEqual(
            seen["urls"], ["http://oss/a.png", "http://oss/b.txt"]
        )
        self.assertIsNone(result)

    def test_skips_without_key(self):
        from bocomadp.deerflow.routers import deerflow_chat as chat_mod

        async def fake_download(*args, **kwargs):
            raise AssertionError("must not download without additional_urls")

        params = {"lang": "zh"}
        body = CreateRunRequest(
            agent_id="a1", session_id="s1", custom_params=params,
        )
        with patch.object(chat_mod, "download_urls_to_session", new=fake_download):
            result = run(
                _download_additional_urls(
                    body, "u1", "a1", "s1", FakeStorage(), FakeWorkspaceManager(),
                )
            )
        # 未携带 additional_urls：不触发下载（落盘由 _resolve_custom_params
        # 对 body.custom_params 整体完成）
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
