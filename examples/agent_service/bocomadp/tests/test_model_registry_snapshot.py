# -*- coding: utf-8 -*-
"""model_registry 进程内快照与同步读函数单测。

用临时文件 sqlite 库替换模块 ``_engine``（与 test_runtime_config.py 一致，
pytest-asyncio 未安装，异步用例统一用 ``asyncio.run()`` 包裹），验证：

- 写接口（create/delete）成功后快照自动刷新；
- :func:`get_model_meta` / :func:`list_model_metas` 同步读取；
- ``EllmChatModel.list_models()`` / ``_get_model_context_size`` /
  ``_get_think_tag`` 从快照读取（不再依赖 Redis / ``_models/*.yaml``）。
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


def test_context_size_and_think_tag_from_snapshot(sqlite_registry):
    _create("m1", think_tag=True, context_size=65536)

    assert ellm_chat_model._get_model_context_size("m1") == 65536
    assert ellm_chat_model._get_think_tag("m1") is True

    # 库中无该模型 → 安全默认
    assert (
        ellm_chat_model._get_model_context_size("missing")
        == ellm_chat_model._FALLBACK_CONTEXT_SIZE
    )
    assert ellm_chat_model._get_think_tag("missing") is False
