# -*- coding: utf-8 -*-
"""memory/__init__.py 轮次触发后台化：恒为 fire-and-forget（不阻塞回复收尾）。"""
from __future__ import annotations

import asyncio

from bocomadp.memory import _pending_extract, _spawn_extract
from bocomadp.memory.config import MemoryRuntimeConfig
from bocomadp.memory.store import MemoryConfig


def _run(coro):
    return asyncio.run(coro)


class _FakeRedis:
    """最小 redis 替身：仅 Task1 后台化测试需要（锁/心跳由 Task2 覆盖）。"""

    def __init__(self) -> None:
        self._strings: dict[str, int] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    async def set(self, key, value, ex=None, nx=False, exat=None) -> bool | None:
        del ex, exat
        if nx and key in self._strings:
            return None
        self._strings[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._strings.pop(key, None) is not None:
                removed += 1
            if self._zsets.pop(key, None) is not None:
                removed += 1
        return removed


class _FakeStorage:
    async def list_messages(self, *a, **kw):  # noqa: N805
        return [], False


def test_spawn_extract_runs_in_background_and_cleans_pending(monkeypatch):
    """恒后台：_spawn_extract 立即返回 Task，run_extract 被异步执行。"""
    calls = []

    async def _fake_run_extract(*args, **kwargs):
        calls.append((args, kwargs))

    import bocomadp.memory.extractor as extractor_mod

    monkeypatch.setattr(extractor_mod, "run_extract", _fake_run_extract)
    redis = _FakeRedis()
    cfg = MemoryConfig(memory_enabled=True)
    rt = MemoryRuntimeConfig()

    async def _scenario():
        # _spawn_extract 内含 asyncio.create_task，须在 running loop 内调用
        # （真实运行在 build_memory_middlewares 的 async 上下文，语义一致）。
        task = _spawn_extract(redis, _FakeStorage(), "u1", "a1", "s1", cfg, rt)
        assert isinstance(task, asyncio.Task)
        assert task in _pending_extract
        await task  # 等到完成
        return task

    task = _run(_scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    # run_extract(redis, storage, user_id, agent_id, session_id, cfg, rt_cfg, ...)
    assert args[2:] == ("u1", "a1", "s1", cfg, rt)
    assert kwargs["recheck_active"] is False
    # done 后自动从 _pending 摘除
    assert task not in _pending_extract


def test_no_await_extract_field():
    """轮次触发提取恒后台：await_extract 同步开关配置已移除。"""
    rt = MemoryRuntimeConfig()
    assert not hasattr(rt, "await_extract")
    # 其余运行参数仍保留默认值
    assert rt.idle_minutes == 15
    assert rt.sweep_interval_seconds == 60
    assert rt.max_tokens == 90000
