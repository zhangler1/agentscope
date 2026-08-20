# -*- coding: utf-8 -*-
"""deerflow custom_params.additional_urls 下载保存功能测试。

覆盖 ``bocomadp.routers.uploads.download_urls_to_session`` /
``_filename_from_url`` 与 ``bocomadp.deerflow.routers.deerflow_chat.
_download_additional_urls``：

- 文件名提取（URL 解码 / 回退）；
- 下载成功 → 落盘（backend） + uploads DB 记录（图片 base64 固化）；
- 单个 URL 失败 / 超限 → 跳过不阻断（部分成功语义）；
- 会话文件数上限 → 停止处理剩余 URL；
- additional_urls 一次性消费（从落盘参数剥离）+ 无 key 时原样透传。

运行：``python -m pytest tests/test_deerflow_additional_urls.py -v``
"""
from __future__ import annotations

import pytest

from bocomadp.deerflow.routers.deerflow_chat import (
    CreateRunRequest,
    _download_additional_urls,
)
from bocomadp.routers.uploads import _filename_from_url, download_urls_to_session
from bocomadp.uploads.db import UploadsDB

# PNG 魔数（1x1 透明像素），用于验证图片 base64 固化分支
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082",
)


class FakeBackend:
    """内存版 backend：join_path / write_file / read_file。"""

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
    """按 URL 返回预置响应的 httpx.AsyncClient 替身。

    urls 中前缀带 ``!`` 的条目抛 :class:`httpx.RequestError`，
    后缀带 ``?<status>`` 的条目返回 :class:`httpx.HTTPStatusError`。
    """

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> None:
        pass

    async def get(self, url: str) -> FakeResponse:
        if url.startswith("!"):
            import httpx

            raise httpx.RequestError("boom", request=httpx.Request("GET", url))
        status = 200
        if "?" in url:
            url, status = url.split("?", 1)
            status = int(status)
        resp = self.responses.get(url)
        if resp is None or status >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "HTTP error",
                request=httpx.Request("GET", url),
                response=httpx.Response(status, request=httpx.Request("GET", url)),
            )
        return resp


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> dict:
    """monkeypatch get_uploads_db + get_upload_config，返回测试依赖。"""
    db = UploadsDB(db_path=":memory:")
    monkeypatch.setattr(
        "bocomadp.routers.uploads.get_uploads_db",
        lambda: db,
    )
    cfg = {
        "enabled": True,
        "max_file_size_mb": 1.0,
        "max_file_size_bytes": 1024 * 1024,
        "max_files_per_session": 50,
    }
    monkeypatch.setattr(
        "bocomadp.routers.uploads.get_upload_config",
        lambda: type("Cfg", (), cfg)(),
    )
    storage = FakeStorage()
    wm = FakeWorkspaceManager()
    return {
        "db": db,
        "cfg": cfg,
        "storage": storage,
        "wm": wm,
        "backend": wm.workspace.backend,
        "user_id": "u1",
        "agent_id": "a1",
        "session_id": "s1",
    }


def _download_sync(deps: dict, urls: list[str]) -> list:
    import asyncio

    return asyncio.get_event_loop().run_until_complete(
        download_urls_to_session(
            deps["user_id"],
            deps["agent_id"],
            deps["session_id"],
            urls,
            deps["storage"],
            deps["wm"],
        ),
    )


# ---------------------------------------------------------------------------
# _filename_from_url
# ---------------------------------------------------------------------------


def test_filename_from_url_basic():
    assert _filename_from_url("http://x.com/a/test.png") == "test.png"
    assert _filename_from_url("https://x.com/path/to/report.pdf") == "report.pdf"


def test_filename_from_url_decodes_and_falls_back():
    # URL 编码解码
    assert _filename_from_url("http://x.com/a/%E6%B5%8B%E8%AF%95.txt") == "测试.txt"
    # 无路径段 / 仅分隔符 → 回退
    assert _filename_from_url("http://x.com/") == "downloaded"
    assert _filename_from_url("http://x.com/a/") == "downloaded"
    assert _filename_from_url("http://x.com/a/..") == "downloaded"


# ---------------------------------------------------------------------------
# download_urls_to_session
# ---------------------------------------------------------------------------


def test_download_saves_files_and_records(deps: dict):
    client = FakeAsyncClient(
        {
            "http://oss/a.png": FakeResponse(PNG_BYTES, "image/png"),
            "http://oss/note.txt": FakeResponse(b"hello", "text/plain"),
        },
    )
    _patch_client(deps, client)

    saved = _download_sync(deps, ["http://oss/a.png", "http://oss/note.txt"])

    assert len(saved) == 2
    # 原始文件落盘到 {workdir}/user-data/uploads/
    assert deps["backend"].files["/ws/user-data/uploads/a.png"] == PNG_BYTES
    assert deps["backend"].files["/ws/user-data/uploads/note.txt"] == b"hello"
    # uploads DB 记录（下游 list_uploaded_files / view_image_tool 感知通道）
    records = deps["db"].list_by_session("u1", "a1", "s1")
    assert len(records) == 2
    img = next(r for r in records if r.stored_name == "a.png")
    assert img.mime_type == "image/png"  # magic bytes 实测
    assert img.base64  # 图片 base64 固化
    assert img.virtual_path == "/workspace/user-data/uploads/a.png"
    txt = next(r for r in records if r.stored_name == "note.txt")
    assert txt.converted is False


def test_download_skips_failed_and_oversized_urls(deps: dict):
    deps["cfg"]["max_file_size_bytes"] = 10  # 1 字节都不够放 12 字节内容
    client = FakeAsyncClient(
        {
            "http://oss/ok.txt": FakeResponse(b"ok"),
            "http://oss/404.txt": FakeResponse(b"nope"),
            "http://oss/big.txt": FakeResponse(b"x" * 12),
        },
    )
    _patch_client(deps, client)

    saved = _download_sync(
        deps,
        ["http://oss/404.txt?404", "!http://oss/timeout.txt", "http://oss/big.txt", "http://oss/ok.txt"],
    )

    # 部分成功：失败 / 超限 URL 跳过，不阻断其余
    assert len(saved) == 1
    assert saved[0].stored_name == "ok.txt"
    records = deps["db"].list_by_session("u1", "a1", "s1")
    assert [r.stored_name for r in records] == ["ok.txt"]


def test_download_respects_file_count_limit(deps: dict):
    deps["cfg"]["max_files_per_session"] = 2
    client = FakeAsyncClient(
        {
            "http://oss/1.txt": FakeResponse(b"1"),
            "http://oss/2.txt": FakeResponse(b"2"),
            "http://oss/3.txt": FakeResponse(b"3"),
        },
    )
    _patch_client(deps, client)

    saved = _download_sync(deps, ["http://oss/1.txt", "http://oss/2.txt", "http://oss/3.txt"])

    assert len(saved) == 2  # 达到上限即 break，剩余 URL 不再下载
    assert [r.stored_name for r in saved] == ["1.txt", "2.txt"]


def test_download_persist_failure_skips_url(deps: dict, monkeypatch: pytest.MonkeyPatch):
    from bocomadp.routers import uploads as uploads_mod

    async def boom(**kwargs):
        from bocomadp.uploads.manager import UploadError

        raise UploadError("write failed")

    monkeypatch.setattr(uploads_mod, "_persist_uploaded_bytes", boom)
    client = FakeAsyncClient({"http://oss/a.txt": FakeResponse(b"a")})
    _patch_client(deps, client)

    saved = _download_sync(deps, ["http://oss/a.txt"])

    assert saved == []  # 保存失败仅告警跳过


# ---------------------------------------------------------------------------
# _download_additional_urls（deerflow_chat）
# ---------------------------------------------------------------------------


def test_download_additional_urls_downloads_cleaned_urls(
    deps: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    from bocomadp.deerflow.routers import deerflow_chat as chat_mod

    seen: dict = {}

    async def fake_download(user_id, agent_id, session_id, urls, storage, wm):
        seen["urls"] = urls
        return []

    monkeypatch.setattr(chat_mod, "download_urls_to_session", fake_download)
    body = CreateRunRequest(
        agent_id="a1",
        session_id="s1",
        custom_params={
            "additional_urls": [" http://oss/a.png ", 123, "", "http://oss/b.txt"],
            "lang": "zh",
        },
    )

    result = _download_additional_urls(
        body,
        "u1", "a1", "s1",
        deps["storage"], deps["wm"],
    )

    # URL 清洗：去空白、过滤非字符串；仅执行下载副作用，返回 None
    # （custom_params 含 additional_urls 整体由 _resolve_custom_params 落盘）
    assert seen["urls"] == ["http://oss/a.png", "http://oss/b.txt"]
    assert result is None


def test_download_additional_urls_skips_without_key(
    deps: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    from bocomadp.deerflow.routers import deerflow_chat as chat_mod

    async def fake_download(*args, **kwargs):  # 不应被调用
        raise AssertionError("must not download without additional_urls")

    monkeypatch.setattr(chat_mod, "download_urls_to_session", fake_download)
    params = {"lang": "zh"}
    body = CreateRunRequest(agent_id="a1", session_id="s1", custom_params=params)

    result = _download_additional_urls(
        body,
        "u1", "a1", "s1",
        deps["storage"], deps["wm"],
    )

    # 未携带 additional_urls：不触发下载（落盘由 _resolve_custom_params
    # 对 body.custom_params 整体完成）
    assert result is None


def _patch_client(deps: dict, client: FakeAsyncClient) -> None:
    import bocomadp.routers.uploads as uploads_mod

    uploads_mod.httpx.AsyncClient = lambda *a, **k: client
