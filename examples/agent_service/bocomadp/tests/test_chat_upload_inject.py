# -*- coding: utf-8 -*-
"""ChatUploadInjectMiddleware「前端显式传 files + 归属校验」的单元测试。

覆盖：合法引用规范化注入、越权（user/session 不匹配）引用丢弃、记录不存在
丢弃、混合引用只保留合法项、前端未传 files 时不改动 payload。
"""
import asyncio
from types import SimpleNamespace

from bocomadp.middleware.chat_upload_inject import (
    _apply_client_files,
    _validate_refs_sync,
)


def _record(
    user_id: str = "u1",
    session_id: str = "s1",
    original_name: str = "报告.md",
    content_type: str | None = "text/markdown",
    virtual_path: str = "/workspace/user-data/uploads/报告.md",
):
    return SimpleNamespace(
        user_id=user_id,
        session_id=session_id,
        original_name=original_name,
        content_type=content_type,
        virtual_path=virtual_path,
    )


class _FakeDB:
    """按 (user_id, session_id, virtual_path) 三键模拟元数据表。"""

    def __init__(self, records: list) -> None:
        self._recs = {
            (r.user_id, r.session_id, r.virtual_path): r for r in records
        }

    def get_by_session_virtual_path(
        self, user_id: str, session_id: str, virtual_path: str
    ):
        return self._recs.get((user_id, session_id, virtual_path))


def _patch_db(monkeypatch, db: _FakeDB) -> None:
    monkeypatch.setattr(
        "bocomadp.middleware.chat_upload_inject.get_uploads_db", lambda: db
    )


def test_valid_ref_normalized(monkeypatch):
    # 合法引用：字段以 DB 记录为准（original_name → filename），多余字段忽略。
    rec = _record()
    _patch_db(monkeypatch, _FakeDB([rec]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {"files": [{"virtual_path": rec.virtual_path, "id": 14}]},
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is True
    assert payload["input"]["metadata"]["files"] == [
        {
            "filename": "报告.md",
            "filetype": "text/markdown",
            "virtual_path": rec.virtual_path,
        }
    ]


def test_cross_user_ref_dropped(monkeypatch):
    # 记录属于别的用户：越权引用必须丢弃，且全部无效时不改动 payload。
    rec = _record(user_id="someone-else")
    _patch_db(monkeypatch, _FakeDB([rec]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {"files": [{"virtual_path": rec.virtual_path}]},
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is False
    assert payload["input"]["metadata"]["files"] == [
        {"virtual_path": rec.virtual_path}
    ]


def test_cross_session_ref_dropped(monkeypatch):
    # 记录属于同用户的其他会话：同样视为越权丢弃。
    rec = _record(session_id="other-session")
    _patch_db(monkeypatch, _FakeDB([rec]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {"files": [{"virtual_path": rec.virtual_path}]},
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is False


def test_duplicate_virtual_path_picks_current_session(monkeypatch):
    # 回归：方案 A 下不同会话上传同名文件产生多条相同 virtual_path 记录，
    # 校验必须命中当前会话那条，而不是被 LIMIT 1 命中的旧记录带偏。
    old = _record(session_id="old-session", content_type="text/plain")
    cur = _record(session_id="s1")
    _patch_db(monkeypatch, _FakeDB([old, cur]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {"files": [{"virtual_path": cur.virtual_path}]},
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is True
    assert payload["input"]["metadata"]["files"] == [
        {
            "filename": "报告.md",
            "filetype": "text/markdown",
            "virtual_path": cur.virtual_path,
        }
    ]


def test_missing_record_dropped(monkeypatch):
    # virtual_path 在元数据表里查不到：丢弃。
    _patch_db(monkeypatch, _FakeDB([]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {"files": [{"virtual_path": "/x/ghost.md"}]},
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is False


def test_mixed_refs_keep_valid_only(monkeypatch):
    # 混合引用：合法项注入，越权项丢弃。
    good = _record()
    bad = _record(
        user_id="u2",
        virtual_path="/workspace/user-data/uploads/别人的.md",
    )
    _patch_db(monkeypatch, _FakeDB([good, bad]))
    payload = {
        "session_id": "s1",
        "input": {
            "role": "user",
            "metadata": {
                "files": [
                    {"virtual_path": good.virtual_path},
                    {"virtual_path": bad.virtual_path},
                ]
            },
        },
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is True
    files = payload["input"]["metadata"]["files"]
    assert len(files) == 1
    assert files[0]["filename"] == "报告.md"


def test_no_files_noop(monkeypatch):
    # 前端未传 files：不做任何注入，payload 原样。
    _patch_db(monkeypatch, _FakeDB([]))
    payload = {
        "session_id": "s1",
        "input": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
    }
    assert asyncio.run(_apply_client_files(payload, "u1")) is False
    assert "metadata" not in payload["input"]


def test_validate_refs_sync_reports_dropped(monkeypatch):
    # 底层校验：非法形态（非 dict / 缺 virtual_path / 查无记录）全部上报。
    rec = _record()
    _patch_db(monkeypatch, _FakeDB([rec]))
    files, dropped = _validate_refs_sync(
        [
            {"virtual_path": rec.virtual_path},
            {"virtual_path": "/x/ghost.md"},
            "not-a-dict",
            {},
        ],
        "u1",
        "s1",
    )
    assert len(files) == 1
    assert files[0]["filename"] == "报告.md"
    assert dropped == ["/x/ghost.md", "not-a-dict", "<missing virtual_path>"]
