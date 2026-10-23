# -*- coding: utf-8 -*-
"""双后端一致性测试（Task 7，§9）：14 方法全覆盖 + 三个专项。

统一形态：同步测试函数内定义 ``async def scenario()``，
``asyncio.run(scenario())`` 收口（SQL 后端为真实 MySQL，用例间由
``_IsolatedSQLStorage`` 清表隔离）。
"""
import asyncio

import pytest

from bocomadp.memory.storage._models import MemoryAgentConfigRecord


def _cfg(agent_id: str = "a1", **kw) -> MemoryAgentConfigRecord:
    return MemoryAgentConfigRecord(agent_id=agent_id, **kw)


class TestAgentConfigCrud:
    def test_upsert_get_list_delete(self, make_storage):
        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            rec = _cfg("a1", extraction_enabled=True, infer="0", top_k=9)
            out = await st.upsert_memory_config(rec)
            assert out.agent_id == "a1"
            got = await st.get_memory_config("a1")
            assert got is not None
            assert got.infer == "0"            # D10 字段在
            assert got.extraction_enabled is True
            assert got.top_k == 9
            lst = await st.list_memory_configs()
            assert [r.agent_id for r in lst] == ["a1"]
            assert await st.delete_memory_config("a1") is True
            assert await st.get_memory_config("a1") is None
            assert await st.delete_memory_config("a1") is False  # 再删返回 False

        asyncio.run(scenario())

    def test_list_memory_configs_paginated(self, make_storage):
        """分页契约：agent_id 升序 + limit/offset（默认 LIMIT 100 防全表扫描）。"""

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            for agent_id in ("a1", "a2", "a3"):
                await st.upsert_memory_config(_cfg(agent_id))
            return (
                await st.list_memory_configs(limit=2),
                await st.list_memory_configs(limit=2, offset=2),
                await st.list_memory_configs(),
            )

        page1, page2, whole = asyncio.run(scenario())
        assert [r.agent_id for r in page1] == ["a1", "a2"]
        assert [r.agent_id for r in page2] == ["a3"]
        assert [r.agent_id for r in whole] == ["a1", "a2", "a3"]


class TestSessionState:
    def test_ensure_touch_get_increment_cursor_reset_delete(self, make_storage):
        """v2 语义：ensure 唯一建档、touch 只更新、increment 不存在 no-op。"""
        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            # touch 对不存在的状态 → None（只更新、不创建）
            assert await st.touch_memory_session("u1", "a1", "s1", now=100.0) is None
            # ensure 建档（唯一创建入口）：游标 + epoch + turns=0
            created = await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-01", cursor_msg_id="m0",
                epoch=2, now=100.0,
            )
            assert created is True
            r = await st.get_memory_session_state("u1", "a1", "s1")
            assert r is not None
            assert r.turns == 0 and r.last_active_at == 100.0
            assert r.switch_epoch == 2
            assert r.cursor_created_at == "2026-01-01" and r.cursor_msg_id == "m0"
            # ensure 已存在 → no-op（不覆盖游标/epoch）
            created2 = await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-02-02", cursor_msg_id="mX",
                epoch=9, now=150.0,
            )
            assert created2 is False
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None
            assert g.switch_epoch == 2 and g.cursor_msg_id == "m0"
            # touch 只刷 last_active_at，不动 turns/cursor/epoch
            r2 = await st.touch_memory_session("u1", "a1", "s1", now=200.0)
            assert r2 is not None
            assert r2.turns == 0 and r2.last_active_at == 200.0
            assert r2.cursor_msg_id == "m0" and r2.switch_epoch == 2
            # increment 连续 3 次：1/2/3
            assert await st.increment_memory_session_turns("u1", "a1", "s1") == 1
            assert await st.increment_memory_session_turns("u1", "a1", "s1") == 2
            assert await st.increment_memory_session_turns("u1", "a1", "s1") == 3
            # set_cursor → 回读
            await st.set_memory_session_cursor("u1", "a1", "s1", "2026-01-01", "m9")
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None
            assert g.cursor_created_at == "2026-01-01"
            assert g.cursor_msg_id == "m9"
            # reset：turns=0 且 cursor 保留
            await st.reset_memory_session_turns("u1", "a1", "s1")
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None
            assert g.turns == 0
            assert g.cursor_msg_id == "m9"
            # reset(turns=1)：尾部那一轮待补齐 → 保留触发动力
            await st.reset_memory_session_turns("u1", "a1", "s1", turns=1)
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None
            assert g.turns == 1
            assert g.cursor_msg_id == "m9"
            # delete
            assert await st.delete_memory_session_state("u1", "a1", "s1") is True
            assert await st.get_memory_session_state("u1", "a1", "s1") is None
            assert await st.delete_memory_session_state("u1", "a1", "s1") is False

        asyncio.run(scenario())

    def test_reset_memory_session_state_upsert(self, make_storage):
        """B9：无条件重写（upsert）——存在则重写、不存在则插入。"""
        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            # 不存在 → 插入
            rec = await st.reset_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-01", cursor_msg_id="m1",
                epoch=5, now=10.0,
            )
            assert rec.turns == 0 and rec.switch_epoch == 5
            assert rec.cursor_msg_id == "m1"
            # 存在 → 重写（turns/epoch/游标全被覆盖）
            await st.increment_memory_session_turns("u1", "a1", "s1")
            rec2 = await st.reset_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-02", cursor_msg_id="m2",
                epoch=6, now=20.0,
            )
            assert rec2.turns == 0 and rec2.switch_epoch == 6
            assert rec2.cursor_msg_id == "m2"
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None and g.switch_epoch == 6

        asyncio.run(scenario())

    def test_increment_missing_state_noop(self, make_storage):
        """increment 对不存在的状态 no-op 返回 0（不创建无游标状态）。"""
        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            assert await st.increment_memory_session_turns("u1", "a1", "s1") == 0
            assert await st.get_memory_session_state("u1", "a1", "s1") is None

        asyncio.run(scenario())

    def test_increment_concurrent(self, make_storage):
        """并发 10 次 increment，结果为 1..10 的集合（原子性）。"""

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=1.0,
            )
            results = await asyncio.gather(*[
                st.increment_memory_session_turns("u1", "a1", "s1")
                for _ in range(10)
            ])
            assert sorted(results) == list(range(1, 11))
            g = await st.get_memory_session_state("u1", "a1", "s1")
            assert g is not None and g.turns == 10

        asyncio.run(scenario())

    def test_session_state_table_contract(self, make_storage):
        """SQL 表契约：自增整型主键 + 身份三元组唯一约束。

        唯一约束是 ``ensure`` 并发兜底（撞唯一键 → 视为已存在）的依据，
        这里显式断言，防止将来改动表结构时静默丢失。
        """
        if make_storage.backend != "sql":
            pytest.skip("sql 专项：表结构断言")
        from sqlalchemy import UniqueConstraint

        from bocomadp.memory.storage._sql_memory import _SessionStateRow

        table = _SessionStateRow.__table__
        uniques = {
            (constraint.name, tuple(sorted(c.name for c in constraint.columns)))
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert (
            "UK1_memory_session_states",
            ("agent_id", "session_id", "user_id"),
        ) in uniques
        pk_columns = list(table.primary_key.columns)
        assert [c.name for c in pk_columns] == ["id"]
        assert pk_columns[0].autoincrement is True
        # 索引一律单列（§3.2）：心跳列供清理/提取的范围查询与排序，
        # agent_id 供 cleanup_agent_memory 等值反查
        indexes = {
            (index.name, tuple(c.name for c in index.columns))
            for index in table.indexes
        }
        assert ("I1_memory_session_states", ("last_active_at",)) in indexes
        assert ("I2_memory_session_states", ("agent_id",)) in indexes
        # turns 提升列：非空 + 库端默认 0（读路径由列拼回，漏配即 fail-fast）
        turns = table.columns["turns"]
        assert turns.nullable is False
        assert turns.server_default is not None
        assert turns.type.python_type is int
        # 建表规范：created_at / updated_at 均非空且带库端默认值
        for name in ("created_at", "updated_at"):
            column = table.columns[name]
            assert column.nullable is False
            assert column.server_default is not None

    def test_last_active_at_utc_datetime_column(self, make_storage):
        """心跳列契约：DB 侧存 naive UTC ``DATETIME`` 字面值，端口侧仍为 float 秒。

        该列是会话心跳的唯一真源（payload 不含 ``last_active_at``）：既服务
        索引范围查询（``last_active_before``），也是读路径拼回 record 的来源
        ——两侧都钉住：既防实现引入本地时区偏移，也防端口契约从 float 秒
        漂移。
        """
        if make_storage.backend != "sql":
            pytest.skip("sql 专项：心跳列存储契约")
        from datetime import datetime, timezone

        from sqlalchemy import DateTime, column, select

        from bocomadp.memory.storage._sql_memory import _SessionStateRow

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = 1759999999.0
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=now,
            )
            # 原始列值：按标准 DateTime 读（绕开本列的适配类型）
            async with st._engine.connect() as conn:
                raw = (
                    await conn.execute(
                        select(
                            column("last_active_at", DateTime)
                        ).select_from(_SessionStateRow.__table__)
                    )
                ).scalar_one()
            expected = datetime.fromtimestamp(
                now, tz=timezone.utc
            ).replace(tzinfo=None)
            # 比较到秒：列类型为 DATETIME(6)（保留微秒），仅比到秒级
            assert raw.replace(microsecond=0) == expected.replace(
                microsecond=0
            )
            # 端口侧读回仍是原始 float 秒（由提升列拼回；整秒值往返无损）
            r = await st.get_memory_session_state("u1", "a1", "s1")
            assert r is not None and r.last_active_at == now
            # 经列类型往返须无损到秒级（绑定/取值偏移不一致会整体偏移）
            async with st._engine.connect() as conn:
                back = (
                    await conn.execute(
                        select(_SessionStateRow.last_active_at)
                    )
                ).scalar_one()
            assert abs(back - now) < 1.0

        asyncio.run(scenario())


class TestCandidateQueryGuard:
    """提取候选查询的入口约束（``last_active_before`` 必填）。"""

    def test_last_active_before_is_required(self, make_storage):
        """省略必填参数 → TypeError：结构上不存在"无条件全表扫描"入口。"""
        st = make_storage()
        with pytest.raises(TypeError):
            st.list_memory_session_states()  # type: ignore[call-arg]


async def _raw_session_row(st, user_id, agent_id, session_id):
    """直读 DB 的 payload + 心跳列 + turns 列（绕开端口契约做真源断言）。"""
    from sqlalchemy import select

    from bocomadp.memory.storage._sql_memory import (
        _SessionStateRow,
        _identity_clause,
    )

    async with st._engine.connect() as conn:
        return (
            await conn.execute(
                select(
                    _SessionStateRow.payload,
                    _SessionStateRow.last_active_at,
                    _SessionStateRow.turns,
                ).where(*_identity_clause(user_id, agent_id, session_id))
            )
        ).one()


class TestSessionPayloadContract:
    """payload 与提升列的「单一真源」契约（SQL 专项）。"""

    def test_payload_excludes_promoted_columns(self, make_storage):
        """写入后 payload 只含非提升字段（身份三元组 + 心跳 + turns 在列）。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：payload 剔除提升列")
        import json

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-01", cursor_msg_id="m1",
                epoch=2, now=100.0,
            )
            row = await _raw_session_row(st, "u1", "a1", "s1")
            assert set(json.loads(row.payload)) == {
                "cursor_created_at",
                "cursor_msg_id",
                "switch_epoch",
            }
            assert row.last_active_at == 100.0
            assert row.turns == 0

        asyncio.run(scenario())

    def test_column_update_is_authoritative(self, make_storage):
        """直接改心跳列即生效（读路径由列拼回）——旁路修正的回归。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：列是真源")
        from sqlalchemy import update

        from bocomadp.memory.storage._sql_memory import (
            _SessionStateRow,
            _identity_clause,
        )

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
            )
            # 模拟外部直接 UPDATE 心跳列（不碰 payload）
            async with st._engine.begin() as conn:
                await conn.execute(
                    update(_SessionStateRow)
                    .where(*_identity_clause("u1", "a1", "s1"))
                    .values(last_active_at=9999.0)
                )
            got = await st.get_memory_session_state("u1", "a1", "s1")
            assert got is not None and got.last_active_at == 9999.0
            # 列是真源：直读列同为 9999.0（列举已收敛为候选查询，不作校验用）
            raw = await _raw_session_row(st, "u1", "a1", "s1")
            assert raw.last_active_at == 9999.0

        asyncio.run(scenario())

    def test_heartbeat_does_not_rewrite_payload(self, make_storage):
        """心跳只刷列：touch 前后 payload 不变。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：payload 剔除提升列")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-01", cursor_msg_id="m1",
                epoch=0, now=100.0,
            )
            before = (await _raw_session_row(st, "u1", "a1", "s1")).payload
            await st.touch_memory_session("u1", "a1", "s1", now=200.0)
            after = await _raw_session_row(st, "u1", "a1", "s1")
            assert after.payload == before
            assert after.last_active_at == 200.0

        asyncio.run(scenario())


class TestTurnsColumn:
    """``turns`` 提升为列（§3.4）：列是真源，且计数不再重写 payload。"""

    def test_turns_column_roundtrip(self, make_storage):
        """``ensure`` → 0；连续 ``increment`` → 1/2/3；``reset`` → 0/1。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：turns 列往返")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
            )
            assert (await _raw_session_row(st, "u1", "a1", "s1")).turns == 0
            got = [
                await st.increment_memory_session_turns("u1", "a1", "s1")
                for _ in range(3)
            ]
            assert got == [1, 2, 3]
            assert (await _raw_session_row(st, "u1", "a1", "s1")).turns == 3
            # 读路径同样由列拼回（payload 不含 turns）
            state = await st.get_memory_session_state("u1", "a1", "s1")
            assert state is not None and state.turns == 3
            await st.reset_memory_session_turns("u1", "a1", "s1")
            assert (await _raw_session_row(st, "u1", "a1", "s1")).turns == 0
            # 「未结清」分支：置 1（extractor 保留下轮触发动力）
            await st.reset_memory_session_turns("u1", "a1", "s1", turns=1)
            assert (await _raw_session_row(st, "u1", "a1", "s1")).turns == 1

        asyncio.run(scenario())

    def test_increment_does_not_rewrite_payload(self, make_storage):
        """计数只写 turns + 心跳列：payload 字节不变（消一处写放大）。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：payload 剔除提升列")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="2026-01-01", cursor_msg_id="m1",
                epoch=2, now=100.0,
            )
            before = (await _raw_session_row(st, "u1", "a1", "s1")).payload
            assert await st.increment_memory_session_turns(
                "u1", "a1", "s1"
            ) == 1
            after = await _raw_session_row(st, "u1", "a1", "s1")
            assert after.payload == before
            assert after.turns == 1
            assert after.last_active_at > 100.0

        asyncio.run(scenario())

    def test_reset_turns_missing_state_is_noop(self, make_storage):
        """行不存在 → no-op（不抛错）：extractor 的"置位失败只多触发"依赖它。"""

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.reset_memory_session_turns("u1", "a1", "s1", turns=0)
            assert await st.get_memory_session_state("u1", "a1", "s1") is None

        asyncio.run(scenario())


class TestDeleteStaleSessionStates:
    """批量删超龄会话状态（§2.2–2.4）：`limit` 生效、逐批推进、返回候选数。"""

    def test_limit_and_progress_until_drained(self, make_storage, session_ids):
        """10 条超龄 + 2 条新鲜：`limit=3` 逐批删到空，新鲜不受影响。"""
        import time

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            threshold = now - 7 * 86400
            for i in range(10):  # s0 最老 → s9 最新（全部超龄）
                await st.ensure_memory_session_state(
                    f"u{i}", "a1", f"s{i}",
                    cursor_created_at="", cursor_msg_id="", epoch=0,
                    now=threshold - 100.0 + i,
                )
            for i in range(2):  # 新鲜：不到阈值
                await st.ensure_memory_session_state(
                    f"f{i}", "a1", f"f{i}",
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=now,
                )
            counts = []
            while True:
                n = await st.delete_stale_session_states(threshold, 3)
                counts.append(n)
                if n < 3:
                    break
            assert counts == [3, 3, 3, 1]
            assert await session_ids(st) == ["f0", "f1"]
            # 幂等：清空后再调返回 0
            assert await st.delete_stale_session_states(threshold, 3) == 0

        asyncio.run(scenario())

    def test_threshold_endpoint_exclusive(self, make_storage, session_ids):
        """心跳恰等于阈值的不算超龄（`<` 语义）——两后端一致。"""

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            edge = 1750000000.0  # 取整秒：DATETIME(6) / double 往返均无损
            await st.ensure_memory_session_state(
                "u1", "a1", "older",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=edge - 1.0,
            )
            await st.ensure_memory_session_state(
                "u2", "a1", "edge",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=edge,
            )
            assert await st.delete_stale_session_states(edge, 10) == 1
            assert await session_ids(st) == ["edge"]

        asyncio.run(scenario())

    def test_concurrent_touch_row_is_skipped(self, make_storage, session_ids):
        """SQL 专项：「查与删之间被 touch」的行不被删，且不产生死锁。

        独立连接先把最老候选的心跳刷成"现在"（持锁不提交）→ 删除事务在
        点删时等待该行锁 → 提交后重估 ``last_active_at < ?`` 不成立 → 跳过。
        返回值是**候选数**（含被跳过的那条），故 ≥ 实际删除行数。
        """
        import time

        if make_storage.backend == "redis":
            pytest.skip("sql 专项：两步点删的并发语义")
        from sqlalchemy import update

        from bocomadp.memory.storage._sql_memory import (
            _SessionStateRow,
            _identity_clause,
        )

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            threshold = now - 7 * 86400
            for name, hb in (
                ("s1", threshold - 300.0),
                ("s2", threshold - 200.0),
                ("s3", threshold - 100.0),
            ):
                await st.ensure_memory_session_state(
                    "u1", "a1", name,
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=hb,
                )
            conn = await st._engine.connect()
            tx = await conn.begin()
            await conn.execute(
                update(_SessionStateRow)
                .where(*_identity_clause("u1", "a1", "s1"))
                .values(last_active_at=now)
            )
            task = asyncio.create_task(
                st.delete_stale_session_states(threshold, 10)
            )
            await asyncio.sleep(0.3)          # 让删除事务卡到 s1 的行锁上
            await tx.commit()
            await conn.close()
            n = await asyncio.wait_for(task, timeout=20)  # 死锁/锁超时即失败
            assert n == 3                    # 候选数（含被跳过的 s1）
            assert await session_ids(st) == ["s1"]

        asyncio.run(scenario())

    def test_redis_cleans_hash_and_index_and_counts_candidates(self, make_storage):
        """Redis 专项：Hash 与 ZSET 一起清；悬空成员只 ZREM 且计入候选数。"""
        import time

        if make_storage.backend == "sql":
            pytest.skip("redis 专项：悬空成员与候选数口径")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            threshold = now - 7 * 86400
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0,
                now=threshold - 100.0,
            )
            # 手工制造悬空成员（Hash 已被 TTL 回收）
            await st._redis.zadd(
                "agentscope:memory:sessions", {"u9:a9:s9": threshold - 200.0}
            )
            assert await st.delete_stale_session_states(threshold, 10) == 2
            assert await st._redis.exists(
                "agentscope:memory:session:u1:a1:s1"
            ) == 0
            members = await st._redis.zrange(
                "agentscope:memory:sessions", 0, -1
            )
            assert [m.decode() for m in members] == []

        asyncio.run(scenario())


class TestAgentSessionCleanup:
    """agent 级会话状态清理（§2.7）：作用域、计数、幂等、悬空/非法成员。"""

    def test_deletes_only_target_agent_and_is_idempotent(
        self, make_storage, session_ids
    ):
        """只删目标 agent；计数 = 该 agent 条数；重复调用幂等返回 0。"""

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            for session in ("s1", "s2"):
                await st.ensure_memory_session_state(
                    "u1", "a1", session,
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
                )
            await st.ensure_memory_session_state(
                "u1", "a2", "s9",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
            )
            # a1 的 s2 置为待提取（进 pending）：两处索引都要被清
            await st.increment_memory_session_turns("u1", "a1", "s2")
            assert await st.delete_agent_session_states("a1") == 2
            assert await session_ids(st) == ["s9"]
            assert await st.delete_agent_session_states("a1") == 0  # 幂等

        asyncio.run(scenario())

    def test_sql_batches_internally(self, make_storage, session_ids, monkeypatch):
        """SQL 专项：单批上限生效时内部循环推进，一次调用删净。"""
        if make_storage.backend == "redis":
            pytest.skip("sql 专项：单批事务上限")

        from bocomadp.memory.storage import _sql_memory as sql_mod

        monkeypatch.setattr(sql_mod, "_DELETE_BATCH", 2)

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            for i in range(5):
                await st.ensure_memory_session_state(
                    f"u{i}", "a1", f"s{i}",
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
                )
            assert await st.delete_agent_session_states("a1") == 5
            assert await session_ids(st) == []

        asyncio.run(scenario())

    def test_redis_collect_then_delete_no_miss(
        self, make_storage, session_ids, monkeypatch
    ):
        """Redis 专项回归：小页 ZSCAN + 收集后删 → 一条不漏。

        边扫边删会因 ZSET 内部 dict 收缩而让游标跳过整块桶（设计文档 §2.7
        实测：8500 成员漏删 1250 条），故本用例把 `_SCAN_PAGE` 压到 3 ——
        若实现退化为"边扫边删"，命中数必然小于目标条数。
        """
        if make_storage.backend == "sql":
            pytest.skip("redis 专项：ZSCAN 收集后删")

        from bocomadp.memory.storage import _redis_memory as redis_mod

        monkeypatch.setattr(redis_mod, "_SCAN_PAGE", 3)

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            for i in range(20):
                await st.ensure_memory_session_state(
                    f"u{i}", "a1", f"s{i}",
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
                )
                await st.ensure_memory_session_state(
                    f"u{i}", "a2", f"t{i}",
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
                )
            assert await st.delete_agent_session_states("a1") == 20
            assert await session_ids(st) == sorted(
                f"t{i}" for i in range(20)
            )

        asyncio.run(scenario())

    def test_redis_dangling_and_invalid_members(self, make_storage):
        """Redis 专项：悬空成员与非法 member 只回收索引，且计入返回条数。"""
        if make_storage.backend == "sql":
            pytest.skip("redis 专项：悬空成员与非法 member")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=100.0,
            )
            await st.increment_memory_session_turns("u1", "a1", "s1")
            redis = st._redis
            # 悬空成员：只在两处索引，Hash 已被 TTL 回收
            await redis.zadd("agentscope:memory:sessions", {"u9:a1:s9": 1.0})
            await redis.zadd("agentscope:memory:pending", {"u9:a1:s9": 1.0})
            # 非法 member（无冒号分隔）
            await redis.zadd("agentscope:memory:sessions", {"badmember": 2.0})
            assert await st.delete_agent_session_states("a1") == 3
            assert await redis.exists("agentscope:memory:session:u1:a1:s1") == 0
            assert await redis.zcard("agentscope:memory:sessions") == 0
            assert await redis.zcard("agentscope:memory:pending") == 0

        asyncio.run(scenario())


class TestExtractCandidateQuery:
    """提取候选查询（§3.4 / §3.6）：`turns >= 1` + `limit` + 心跳升序。"""

    def test_pending_candidates_ordered_with_limit(self, make_storage):
        """只回 `turns >= 1`；按心跳升序；`limit` 生效。

        构造"更老但已提取完（turns=0）"的行堵在前面 —— 两端都必须精确
        跳过它（SQL 靠 `WHERE turns >= 1`，Redis 靠 pending 索引）。
        """
        import time

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            silent = now - 3600
            for name, hb, turns in (
                ("z0", silent - 300.0, 0),   # 更老但已结清
                ("p0", silent - 200.0, 1),
                ("p1", silent - 100.0, 2),
            ):
                await st.ensure_memory_session_state(
                    "u1", "a1", name,
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=hb,
                )
                for _ in range(turns):
                    await st.increment_memory_session_turns(
                        "u1", "a1", name
                    )
                await st.touch_memory_session("u1", "a1", name, now=hb)
            first = await st.list_memory_session_states(
                last_active_before=now, limit=1
            )
            assert [r.session_id for r in first] == ["p0"]
            rest = await st.list_memory_session_states(
                last_active_before=now, limit=10
            )
            assert [r.session_id for r in rest] == ["p0", "p1"]

        asyncio.run(scenario())


class TestRedisPendingIndex:
    """Redis 专项（§3.5）：pending 副索引不变量、自愈、悬空清理。"""

    SESSIONS = "agentscope:memory:sessions"
    PENDING = "agentscope:memory:pending"

    @staticmethod
    async def _members(st, key: str) -> set[str]:
        raw = await st._redis.zrange(key, 0, -1)
        return {m.decode() if isinstance(m, bytes) else m for m in raw}

    def test_invariant_holds_across_writes(self, make_storage):
        """走一遍写路径后，pending 成员集合 == `turns >= 1` 的成员集合。"""
        import time

        if make_storage.backend == "sql":
            pytest.skip("redis 专项：pending 副索引")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            for name in ("s1", "s2", "s3"):
                await st.ensure_memory_session_state(
                    "u1", "a1", name,
                    cursor_created_at="", cursor_msg_id="", epoch=0, now=now,
                )
            assert await self._members(st, self.PENDING) == set()  # 建档 turns=0
            await st.increment_memory_session_turns("u1", "a1", "s1")
            await st.increment_memory_session_turns("u1", "a1", "s2")
            await st.increment_memory_session_turns("u1", "a1", "s2")
            assert await self._members(st, self.PENDING) == {
                "u1:a1:s1", "u1:a1:s2",
            }
            # touch：turns>=1 的留在内（刷 score），turns=0 的不进
            await st.touch_memory_session("u1", "a1", "s2", now=now + 1)
            await st.touch_memory_session("u1", "a1", "s3", now=now + 1)
            assert await self._members(st, self.PENDING) == {
                "u1:a1:s1", "u1:a1:s2",
            }
            # 结清 → 出 pending；未结清（置 1）→ 留在内
            await st.reset_memory_session_turns("u1", "a1", "s1", turns=0)
            assert await self._members(st, self.PENDING) == {"u1:a1:s2"}
            await st.reset_memory_session_turns("u1", "a1", "s2", turns=1)
            assert await self._members(st, self.PENDING) == {"u1:a1:s2"}
            # 删会话状态 → 一并清（s2 两处都没了；s1/s3 的状态仍在全集索引）
            await st.delete_memory_session_state("u1", "a1", "s2")
            assert await self._members(st, self.PENDING) == set()
            assert await self._members(st, self.SESSIONS) == {
                "u1:a1:s1", "u1:a1:s3",
            }

        asyncio.run(scenario())

    def test_missing_member_self_heals_on_next_write(self, make_storage):
        """模拟写失败（手工 ZREM）→ 下一次 touch/increment 把它补回。"""
        import time

        if make_storage.backend == "sql":
            pytest.skip("redis 专项：pending 副索引")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            now = time.time()
            await st.ensure_memory_session_state(
                "u1", "a1", "s1",
                cursor_created_at="", cursor_msg_id="", epoch=0, now=now,
            )
            await st.increment_memory_session_turns("u1", "a1", "s1")
            await st._redis.zrem(self.PENDING, "u1:a1:s1")  # 模拟写失败
            assert await self._members(st, self.PENDING) == set()
            await st.touch_memory_session("u1", "a1", "s1", now=now + 1)
            assert await self._members(st, self.PENDING) == {"u1:a1:s1"}

        asyncio.run(scenario())

    def test_dangling_pending_member_dropped_on_read(self, make_storage):
        """member 在 pending 但 Hash 不存在 → 读取候选时清掉且不进结果。"""
        if make_storage.backend == "sql":
            pytest.skip("redis 专项：pending 副索引")

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st._redis.zadd(self.PENDING, {"u9:a9:s9": 1.0})
            got = await st.list_memory_session_states(
                last_active_before=9e9, limit=10
            )
            assert got == []
            assert await self._members(st, self.PENDING) == set()
            assert await self._members(st, self.SESSIONS) == set()

        asyncio.run(scenario())


class TestAgentConfigPayloadContract:
    """agent 配置 payload 与主键列的「单一真源」契约（§3.8 / Task 9）。"""

    @staticmethod
    async def _raw_config(st, backend: str, agent_id: str) -> str:
        if backend == "sql":
            from sqlalchemy import select

            from bocomadp.memory.storage._sql_memory import _AgentConfigRow

            async with st._engine.connect() as conn:
                return (
                    await conn.execute(
                        select(_AgentConfigRow.payload).where(
                            _AgentConfigRow.agent_id == agent_id
                        )
                    )
                ).scalar_one()
        raw = await st._redis.get(f"agentscope:memory:config:{agent_id}")
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def test_payload_excludes_agent_id(self, make_storage):
        """写入后 payload / value 里不含 agent_id（身份只在 PK 列 / 键名）。"""
        import json

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.upsert_memory_config(_cfg("a1", top_k=9))
            raw = await self._raw_config(st, make_storage.backend, "a1")
            assert "agent_id" not in json.loads(raw)
            # 读路径仍能拼回身份（列 / 入参是真源）
            got = await st.get_memory_config("a1")
            assert got is not None and got.agent_id == "a1"
            assert got.top_k == 9
            listed = await st.list_memory_configs()
            assert [r.agent_id for r in listed] == ["a1"]

        asyncio.run(scenario())

    def test_legacy_payload_with_agent_id_is_overridden(self, make_storage):
        """老格式（payload 里带 agent_id，值写成错的）→ 读出以列 / 入参为准。"""
        import json

        async def scenario():
            st = make_storage()
            await st.ensure_ready()
            await st.upsert_memory_config(_cfg("a1", top_k=3))  # 先建档（有行）
            legacy = json.loads(_cfg("a1", top_k=3).model_dump_json())
            legacy["agent_id"] = "WRONG"
            legacy_raw = json.dumps(legacy)
            if make_storage.backend == "sql":
                from sqlalchemy import update

                from bocomadp.memory.storage._sql_memory import _AgentConfigRow

                async with st._engine.begin() as conn:
                    await conn.execute(
                        update(_AgentConfigRow)
                        .where(_AgentConfigRow.agent_id == "a1")
                        .values(payload=legacy_raw)
                    )
            else:
                await st._redis.set(
                    "agentscope:memory:config:a1", legacy_raw
                )
            got = await st.get_memory_config("a1")
            assert got is not None
            assert got.agent_id == "a1"        # 覆盖 payload 里的 WRONG
            assert got.top_k == 3

        asyncio.run(scenario())
