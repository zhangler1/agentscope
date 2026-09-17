# -*- coding: utf-8 -*-
"""model_registry 进程内快照与同步读函数单测。

用临时文件 sqlite 库替换模块 ``_engine``（与 test_runtime_config.py 一致，
pytest-asyncio 未安装，异步用例统一用 ``asyncio.run()`` 包裹），验证：

- 写接口（create/delete）成功后快照自动刷新；
- :func:`get_model_meta` / :func:`list_model_metas` 同步读取；
- ``EllmChatModel.list_models()`` / ``_get_model_context_size`` 从快照读取
  （不再依赖 Redis / ``_models/*.yaml``）；
- :func:`resolve_model_meta` 直读 DB、写穿快照、异常回退（Task1 新增）。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bocomadp.providers import ellm_chat_model
from bocomadp.routers import model_registry
from bocomadp.routers.model_registry import ModelRegistryCreateRequest


@pytest.fixture
def sqlite_registry(tmp_path, monkeypatch):
    """替换 model_registry 的 engine 为临时 sqlite 库并建表、清空快照。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mr.db'}")
    monkeypatch.setattr(model_registry, "_engine", engine)
    monkeypatch.setattr(model_registry, "_snapshot", {})
    asyncio.run(model_registry._ensure_table())
    return engine


def _create(name: str, **kwargs):
    body = ModelRegistryCreateRequest(
        model_name=name,
        context_size=kwargs.pop("context_size", 1000000),
        **kwargs,
    )
    return asyncio.run(model_registry.create_model(body, user_id="u1"))


def test_write_refreshes_snapshot(sqlite_registry):
    """create 成功后快照立即可同步读取。"""
    _create("m1", think_tag=True, output_size=384000)

    meta = model_registry.get_model_meta("m1")
    assert meta is not None
    assert meta["context_size"] == 1000000
    assert meta["think_tag"] in (True, 1)

    metas = model_registry.list_model_metas()
    assert [m["model_name"] for m in metas] == ["m1"]


def test_delete_refreshes_snapshot(sqlite_registry):
    _create("m1")
    assert model_registry.get_model_meta("m1") is not None

    asyncio.run(model_registry.delete_model("m1", user_id="u1"))
    assert model_registry.get_model_meta("m1") is None
    assert model_registry.list_model_metas() == []


def test_list_models_maps_rows_to_cards(sqlite_registry):
    _create("m-think", think_tag=True, context_size=1000000, output_size=384000)
    _create("m-plain", think_tag=False, context_size=32000, output_size=8000)

    cards = {c.name: c for c in ellm_chat_model.EllmChatModel.list_models()}
    assert set(cards) == {"m-think", "m-plain"}
    assert cards["m-think"].output_types == [
        "text/plain",
        "application/x-thinking",
    ]
    assert cards["m-plain"].output_types == ["text/plain"]
    assert cards["m-plain"].context_size == 32000
    assert cards["m-plain"].output_size == 8000


def test_context_size_from_snapshot(sqlite_registry):
    _create("m1", think_tag=True, context_size=65536)

    assert ellm_chat_model._get_model_context_size("m1") == 65536

    # 库中无该模型 → 安全默认
    assert (
        ellm_chat_model._get_model_context_size("missing")
        == ellm_chat_model._FALLBACK_CONTEXT_SIZE
    )


# ---------------------------------------------------------------------------
# Task1：运行时读取（async 直读 DB + 写穿快照 + 异常回退）
# ---------------------------------------------------------------------------


async def _bump_context_size(name: str, size: int) -> None:
    """只用 SQL 改库，不经过写接口（因此不会刷新快照）。"""
    from sqlalchemy import text

    engine = await model_registry._get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE model_registry SET context_size = :size "
                "WHERE model_name = :name",
            ),
            {"size": size, "name": name},
        )


async def _must_not_be_called_async(model_name: str):
    raise AssertionError("同步入口不应触达 DB")


def test_resolve_model_meta_reads_db_and_writes_through(sqlite_registry):
    """直读拿到库里的新值，并把它写穿到快照。"""
    _create("m1", context_size=100_000)
    asyncio.run(_bump_context_size("m1", 200_000))

    # 前提：快照仍是旧值（SQL 直改库不会触发 load_snapshot）
    assert model_registry.get_model_meta("m1")["context_size"] == 100_000

    row = asyncio.run(model_registry.resolve_model_meta("m1"))

    assert row is not None
    assert row["context_size"] == 200_000
    # 写穿：快照被纠正
    assert model_registry.get_model_meta("m1")["context_size"] == 200_000


def test_resolve_model_meta_falls_back_to_snapshot(
    monkeypatch,
    sqlite_registry,
    caplog,
):
    """DB 异常 → 回到快照值；每次异常都打 warning（不节流）。"""
    import logging

    _create("m1", context_size=100_000, think_tag=True)

    async def _boom(model_name: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_registry, "_fetch_one", _boom)

    with caplog.at_level(
        logging.WARNING,
        logger="bocomadp.routers.model_registry",
    ):
        first = asyncio.run(model_registry.resolve_model_meta("m1"))
        second = asyncio.run(model_registry.resolve_model_meta("m1"))

    assert first is not None and first["context_size"] == 100_000
    assert second is not None and second["think_tag"] in (True, 1)
    assert caplog.text.count("falling back to snapshot") == 2


def test_resolve_model_meta_none_when_nothing_known(monkeypatch, sqlite_registry):
    """DB 异常且快照里也没有 → 返回 None（由调用方套安全默认）。"""

    async def _boom(model_name: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_registry, "_fetch_one", _boom)

    assert asyncio.run(model_registry.resolve_model_meta("nope")) is None


def test_resolve_model_meta_missing_row_does_not_touch_snapshot(sqlite_registry):
    """库中确实没有该模型（成功但 None）→ 返回 None，且不动快照。"""
    _create("m1", context_size=100_000)

    assert asyncio.run(model_registry.resolve_model_meta("m2")) is None
    assert model_registry.get_model_meta("m2") is None
    assert list(model_registry._snapshot) == ["m1"]


def test_resolve_model_meta_is_async():
    """护栏：它必须是 async，杜绝将来被改成同步实现。"""
    import inspect

    assert inspect.iscoroutinefunction(model_registry.resolve_model_meta)


def test_sync_entries_do_not_touch_db(monkeypatch, sqlite_registry):
    """护栏：两个同步入口（构造 / list_models）不触 DB，改由快照供数。"""
    from bocomadp.credential import ELLMCredential

    _create("m1", context_size=100_000, think_tag=True)
    monkeypatch.setattr(
        model_registry,
        "_fetch_one",
        _must_not_be_called_async,
    )

    assert ellm_chat_model._get_model_context_size("m1") == 100_000

    model = ellm_chat_model.EllmChatModel(
        credential=ELLMCredential(api_key="k", base_url="http://x", model=None),
        model="m1",
    )
    assert model.context_size == 100_000
    assert {c.name for c in ellm_chat_model.EllmChatModel.list_models()} == {"m1"}
