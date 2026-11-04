# -*- coding: utf-8 -*-
"""并发上限配置层（``chat_concurrency_config``）Redis 降级语义测试。

覆盖的行为：

- Redis 可用 → 以 Redis 为准（多副本共享的新值）；
- Redis 不可用 → 回落进程内缓存（限流继续生效）；
- 两边都没有 → 不限（``None``）；
- Redis 故障后进入冷却，冷却期内不再打网络；
- 冷却结束后自动重试，Redis 恢复即自动回热；
- 写入时 Redis 故障 → 进程内缓存仍写入（PG 已持久化的兜底语义）；
- 查询接口的 ``source`` 在两种故障状态下取值正确。

pytest-asyncio 未安装：异步逻辑用 ``asyncio.run()`` 包裹（与既有测试一致）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import time

import pytest

from bocomadp import chat_concurrency_config


class _FakeRedis:
    """极简 Redis 替身：只实现本模块用到的 hget/hset/hdel。"""

    def __init__(
        self,
        values: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        self.values = dict(values or {})
        self.fail = fail
        self.calls: dict[str, int] = {"hget": 0, "hset": 0, "hdel": 0}

    async def hget(self, key: str, field: str):
        self.calls["hget"] += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self.values.get(key)

    async def hset(self, key: str, field: str, value: str):
        self.calls["hset"] += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.values[key] = value
        return 1

    async def hdel(self, key: str, field: str):
        self.calls["hdel"] += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.values.pop(key, None)
        return 1

    async def scan(self, cursor: int, match: str | None = None, count: int | None = None):
        self.calls["scan"] = self.calls.get("scan", 0) + 1
        if self.fail:
            raise ConnectionError("redis down")
        if match:
            keys = [k for k in self.values if fnmatch.fnmatch(k, match)]
        else:
            keys = list(self.values)
        # 测试数据量小，一次返回全部（cursor=0 表示迭代结束）
        return 0, keys


def _key(agent_id: str) -> str:
    return f"agentscope:chat_concurrency:{agent_id}"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个用例从干净状态开始（缓存空、无冷却、无 PG 记录）。"""
    monkeypatch.setattr(chat_concurrency_config, "_cache", {})
    monkeypatch.setattr(chat_concurrency_config, "_redis_unavailable_until", 0.0)
    chat_concurrency_config._approved_cache.clear()

    async def _pg_none(agent_id: str):
        return None

    monkeypatch.setattr(chat_concurrency_config, "pg_get_full", _pg_none)


def _use_redis(monkeypatch, fake: _FakeRedis) -> None:
    """把模块的 Redis 获取函数换成指定替身。"""

    async def _get_redis():
        return fake

    monkeypatch.setattr(chat_concurrency_config, "_get_redis", _get_redis)


def _run(coro):
    return asyncio.run(coro)


# ── 读路径 ────────────────────────────────────────────────────────


def test_runtime_get_prefers_redis_value(monkeypatch):
    """Redis 可用时以 Redis 为准（本地副本是旧值也不影响）。"""
    fake = _FakeRedis({_key("ag-1"): "7"})
    _use_redis(monkeypatch, fake)
    chat_concurrency_config._cache["ag-1"] = 3
    assert _run(chat_concurrency_config.runtime_get("ag-1")) == 7


def test_runtime_get_falls_back_to_cache_when_redis_down(monkeypatch, caplog):
    """Redis 不可用 → 回落进程内缓存（限流继续生效），并留下告警日志。"""
    fake = _FakeRedis(fail=True)
    _use_redis(monkeypatch, fake)
    chat_concurrency_config._cache["ag-1"] = 3
    with caplog.at_level(logging.WARNING):
        assert _run(chat_concurrency_config.runtime_get("ag-1")) == 3
    assert "redis unavailable" in caplog.text


def test_runtime_get_unlimited_when_no_source(monkeypatch):
    """Redis 不可用且缓存为空 → 不限（None）。"""
    fake = _FakeRedis(fail=True)
    _use_redis(monkeypatch, fake)
    assert _run(chat_concurrency_config.runtime_get("ag-1")) is None


# ── 故障冷却与自动回热 ────────────────────────────────────────────


def test_cooldown_skips_redis_until_expired(monkeypatch):
    """冷却期内不再打 Redis（避免每个请求都白等一次连接超时）。"""
    fake = _FakeRedis(fail=True)
    _use_redis(monkeypatch, fake)
    chat_concurrency_config._cache["ag-1"] = 3
    assert _run(chat_concurrency_config.runtime_get("ag-1")) == 3
    assert fake.calls["hget"] == 1
    assert _run(chat_concurrency_config.runtime_get("ag-1")) == 3
    assert fake.calls["hget"] == 1


def test_recovers_after_cooldown(monkeypatch):
    """冷却结束 + Redis 恢复 → 自动读回新值（无需重启进程）。"""
    down = _FakeRedis(fail=True)
    _use_redis(monkeypatch, down)
    chat_concurrency_config._cache["ag-1"] = 3
    assert _run(chat_concurrency_config.runtime_get("ag-1")) == 3

    monkeypatch.setattr(chat_concurrency_config, "_redis_unavailable_until", 0.0)
    up = _FakeRedis({_key("ag-1"): "9"})
    _use_redis(monkeypatch, up)
    assert _run(chat_concurrency_config.runtime_get("ag-1")) == 9
    assert up.calls["hget"] == 1


# ── 写路径 ────────────────────────────────────────────────────────


def test_set_runtime_writes_cache_even_when_redis_down(monkeypatch):
    """Redis 故障时写入仍落进程内缓存（PG 已持久化的兜底语义）。"""
    fake = _FakeRedis(fail=True)
    _use_redis(monkeypatch, fake)
    assert _run(chat_concurrency_config.set_runtime("ag-1", 4)) is False
    assert chat_concurrency_config._cache["ag-1"] == 4


# ── 查询接口的 source ─────────────────────────────────────────────


def test_get_effective_source(monkeypatch):
    """Redis 有值 → source=redis；Redis 挂 → 回落缓存 source=pg。"""
    up = _FakeRedis({_key("ag-1"): "6"})
    _use_redis(monkeypatch, up)
    assert _run(chat_concurrency_config.get_effective("ag-1")) == (6, "redis")

    down = _FakeRedis(fail=True)
    _use_redis(monkeypatch, down)
    chat_concurrency_config._cache["ag-1"] = 5
    assert _run(chat_concurrency_config.get_effective("ag-1")) == (5, "pg")


# ── 启动回填 ──────────────────────────────────────────────────────


def test_load_runtime_fills_cache_when_redis_down(monkeypatch):
    """Redis 故障时启动回填仍完成（缓存就绪），不抛异常。"""

    async def _pg_all():
        return {"ag-1": (2, time.time(), "ag-1-name")}

    monkeypatch.setattr(chat_concurrency_config, "pg_list_all", _pg_all)
    down = _FakeRedis(fail=True)
    _use_redis(monkeypatch, down)
    _run(chat_concurrency_config.load_runtime())
    assert chat_concurrency_config._cache["ag-1"] == 2


# ── 审核状态门禁（effective_limit） ──────────────────────────────


def _use_market(monkeypatch, on_market=None, error=None):
    """把 agent_market 名单查询换成可控替身（记录调用次数）。"""
    calls = {"n": 0}
    table = on_market or {}

    async def _pg(agent_id: str) -> bool:
        calls["n"] += 1
        if error is not None:
            raise error
        return table.get(agent_id, False)

    monkeypatch.setattr(chat_concurrency_config, "pg_agent_on_market", _pg)
    return calls


def test_effective_limit_non_approved_capped_at_one(monkeypatch):
    """未通过审批（名单无行）→ 硬上限 1，配置了更高值也忽略。

    前端只允许对审核通过的智能体配置并发，此处兜底防御脏数据。"""
    _use_market(monkeypatch, {"ag-1": False})
    fake = _FakeRedis({_key("ag-1"): "5"})
    _use_redis(monkeypatch, fake)
    assert _run(chat_concurrency_config.effective_limit("ag-1")) == 1


def test_effective_limit_approved_uses_config(monkeypatch):
    """已通过审批 → 配置值生效；未配置 = 不限（None）。"""
    _use_market(monkeypatch, {"ag-1": True})
    fake = _FakeRedis({_key("ag-1"): "5"})
    _use_redis(monkeypatch, fake)
    assert _run(chat_concurrency_config.effective_limit("ag-1")) == 5

    empty = _FakeRedis({})
    _use_redis(monkeypatch, empty)
    assert _run(chat_concurrency_config.effective_limit("ag-1")) is None


def test_is_agent_approved_caches_within_ttl(monkeypatch):
    """TTL 内名单查询只打一次库（便签过期前零查库）；过期后重新查询。"""
    calls = _use_market(monkeypatch, {"ag-1": True})
    assert _run(chat_concurrency_config.is_agent_approved("ag-1")) is True
    assert _run(chat_concurrency_config.is_agent_approved("ag-1")) is True
    assert calls["n"] == 1
    # 把 TTL 拨成 0 并清掉便签 → 下次调用视为过期重新查库（验证 TTL 生效）
    monkeypatch.setattr(chat_concurrency_config, "_APPROVED_TTL_SECS", 0.0)
    chat_concurrency_config._approved_cache.clear()
    assert _run(chat_concurrency_config.is_agent_approved("ag-1")) is True
    assert calls["n"] == 2


def test_is_agent_approved_fail_open_on_db_error(monkeypatch, caplog):
    """名单查询异常 → 宽口径视为已通过（不因状态查询阻断对话）。"""
    calls = _use_market(monkeypatch, error=ConnectionError("db down"))
    with caplog.at_level(logging.WARNING):
        assert _run(chat_concurrency_config.is_agent_approved("ag-1")) is True
    assert calls["n"] == 1
    assert "treat as approved" in caplog.text
