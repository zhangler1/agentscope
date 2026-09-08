# -*- coding: utf-8 -*-
"""tool_result_store 单元测试(Redis 用 fake,不依赖真实 Redis)。"""
from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from bocomadp.config.app_config import ToolResultConfig
from bocomadp import tool_result_store as trs


class _FakeRedis:
    """最小 fake:set/get/hgetall/pipeline/expire,带 async 语义。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.hash_data: dict[str, dict[str, str]] = {}
        self.expirations: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value
        if ex is not None:
            self.expirations[key] = ex

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hash_data.get(key, {}))

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.execute()

    async def hset(self, key: str, field: str, value: str) -> None:
        self._ops.append(("hset", key, field, value))

    async def expire(self, key: str, seconds: int) -> None:
        self._ops.append(("expire", key, seconds))

    async def execute(self) -> None:
        for op in self._ops:
            if op[0] == "hset":
                bucket = self._redis.hash_data.setdefault(op[1], {})
                bucket[op[2]] = op[3]
            elif op[0] == "expire":
                self._redis.expirations[op[1]] = op[2]


class TestToolResultStore(IsolatedAsyncioTestCase):
    def _patch_redis(self) -> _FakeRedis:
        fake = _FakeRedis()
        patcher = patch.object(trs, "_get_redis", new=AsyncMock(return_value=fake))
        self.addCleanup(patcher.stop)
        patcher.start()
        return fake

    def test_build_key_format(self):
        assert trs.build_key("s1", "t1") == "tool_result:s1:t1"

    async def test_set_tool_result_uses_ttl_from_config(self):
        fake = self._patch_redis()
        with patch.object(
            trs, "get_tool_result_config", new=AsyncMock(return_value=ToolResultConfig(ttl_seconds=999))
        ):
            key = await trs.set_tool_result("s1", "t1", "hello")

        assert key == "tool_result:s1:t1"
        assert fake.data["tool_result:s1:t1"] == "hello"
        assert fake.expirations["tool_result:s1:t1"] == 999

    async def test_set_tool_result_default_ttl_when_db_missing(self):
        fake = self._patch_redis()
        with patch.object(
            trs, "get_tool_result_config", new=AsyncMock(return_value=ToolResultConfig())
        ):
            await trs.set_tool_result("s1", "t1", "x")

        assert fake.expirations["tool_result:s1:t1"] == trs.DEFAULT_TTL_SECONDS

    async def test_get_tool_result_roundtrip_and_other_session_invisible(self):
        self._patch_redis()
        await trs.set_tool_result("s1", "t1", "payload")
        assert await trs.get_tool_result("s1", "t1") == "payload"
        assert await trs.get_tool_result("s2", "t1") is None  # 其他会话不可见

    async def test_replacement_state_roundtrip(self):
        fake = self._patch_redis()
        await trs.set_replacement_state("s1", {"a": "replacement-text", "b": ""})
        state = await trs.get_replacement_state("s1")
        assert state == {"a": "replacement-text", "b": ""}
        assert await trs.get_replacement_state("s9") == {}
        assert fake.expirations.get("tool_result:replacement:s1") is not None

    async def test_replacement_state_refreshes_ttl(self):
        fake = self._patch_redis()
        with patch.object(
            trs, "get_tool_result_config", new=AsyncMock(return_value=ToolResultConfig(ttl_seconds=777))
        ):
            await trs.set_replacement_state("s1", {"a": "x"})

        assert fake.expirations["tool_result:replacement:s1"] == 777
