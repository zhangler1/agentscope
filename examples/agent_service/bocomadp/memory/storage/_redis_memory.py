# -*- coding: utf-8 -*-
"""MemoryStorage 的 Redis 实现：键设计、读写与并发守卫。

键（前缀 ``agentscope:memory:*``，模板定义在记忆包内）：

============================  ======  ==========================================
键                            类型    用途
============================  ======  ==========================================
agentscope:memory:sessions    ZSET    **全集**范围索引（member=身份三元组，
                                      score=last_active_at）—— 超龄清理 /
                                      按 agent 清理（ZSCAN 扫 member 前缀）/
                                      悬空成员回收
agentscope:memory:pending     ZSET    **待提取**副索引（member 同上，score 同
                                      为心跳）—— 不变量 ``member ∈ pending
                                      ⟺ Hash.turns >= 1``；提取候选查询
                                      （``list_memory_session_states``）
                                      由它精确命中（§3.5），消除"扫全量再过滤"
agentscope:memory:session:    Hash    会话状态主数据（turns / cursor_* /
{u}:{a}:{s}                           switch_epoch / last_active_at），
                                      EXPIRE = state_ttl_secs（每次写入刷新）
agentscope:memory:config:     String  agent 配置（JSON；**value 不含
{agent_id}                            agent_id**——身份在键名里）
agentscope:memory:configs     Set     配置键索引（SREM 返回值兼存在性反馈）
============================  ======  ==========================================

设计要点：

1. **索引是副本**：``sessions`` / ``pending`` 的 member 与 score 都是 Hash
   的副本（Redis 没有二级索引，只能这样模拟）。所以：所有写点必须同写；
   读路径遇到不一致要能自愈（悬空成员惰性 ZREM）；两处索引与 Hash 键的
   构造 / 解析共用 ``_member`` 与 ``member.split(":", 2)``。
2. **TTL 只管 Hash**：Redis 的集合成员没有逐元素 TTL，故索引成员不过期，
   靠读写路径惰性清理 + sweeper 的批量删主动回收。
3. **主动遗忘，不依赖过期事件**：Redis 的过期通知走 pub/sub 且受惰性过期
   影响（不保证及时/不丢），故"超龄遗忘"由 sweeper 按 ``sessions`` 的
   score 扫出来批删；Hash 的 TTL 只作兜底。
4. 错误统一向上抛（fail-fast）。

与 SQL 后端的对称：本后端 Hash 即完整主数据（含 ``last_active_at`` /
``turns``），ZSET 是它的索引副本；SQL 侧对应物是「提升列 + payload（剔除了
提升列）」——存储形态不同，端口契约一致。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ._models import MemoryAgentConfigRecord, MemorySessionStateRecord
from ._port import MemoryStorage

logger = logging.getLogger("as")

_SESSIONS_ZSET = "agentscope:memory:sessions"
_SESSION_HASH = "agentscope:memory:session:{user_id}:{agent_id}:{session_id}"
#: 待提取副索引（§3.5）：member 与 ``sessions`` 相同，score 同为心跳；
#: 不变量 ``member ∈ pending ⟺ Hash.turns >= 1`` —— 让 ``turns >= 1`` 这个
#: 过滤条件也能"下推"（ZSET 里放不下 turns，故为其单建一个索引）。
_PENDING_ZSET = "agentscope:memory:pending"
#: agent 级清理（``delete_agent_session_states``）的 ZSCAN 单页条数
#: （``COUNT`` 只是 hint，实际页大小不保证）。
_SCAN_PAGE = 1000
#: 单次 pipeline 覆盖的成员数（每个成员 3 条命令：DEL + ZREM×2）。
_DELETE_CHUNK = 1000
_CONFIG_KEY = "agentscope:memory:config:{agent_id}"
_CONFIGS_SET = "agentscope:memory:configs"

_HASH_FIELDS = (
    "turns",
    "cursor_created_at",
    "cursor_msg_id",
    "switch_epoch",
    "last_active_at",
)

# 并发安全性说明：touch/ensure/increment 用 EXISTS 守卫
# 实现"只更新/不存在才建/不存在 no-op"。EXISTS→写入之间的残余竞态窗口
# 分析：ensure 的写入为全字段（含游标），即使双写也只是同构覆盖；increment
# 的 HINCRBY 隐式创建要求"EXISTS=True 后、写入前记录恰被删"——TTL 删除
# 要求 7 天静默，而 increment 只发生在心跳新鲜的会话上，两者互斥，实际
# 不可达。同会话并发由框架 session_lock（跨进程分布式锁）排除。


def _session_hash(user_id: str, agent_id: str, session_id: str) -> str:
    return _SESSION_HASH.format(
        user_id=user_id, agent_id=agent_id, session_id=session_id
    )


def _member(user_id: str, agent_id: str, session_id: str) -> str:
    """ZSET member：身份三元组拼接（sessions / pending 两个索引共用）。"""
    return f"{user_id}:{agent_id}:{session_id}"


def _config_key(agent_id: str) -> str:
    return _CONFIG_KEY.format(agent_id=agent_id)


class RedisMemoryStorage(MemoryStorage):
    """Redis 后端。

    Args:
        redis: async Redis client（框架 ``RedisStorage.get_client()`` 传入）。
        state_ttl_secs: 会话 Hash TTL（秒，来自 ``MemoryGlobalConfig``）。
    """

    def __init__(self, redis: Any, state_ttl_secs: float) -> None:
        self._redis = redis
        self._state_ttl = int(state_ttl_secs)

    async def ensure_ready(self) -> None:
        """与 SQL 侧对称的幂等就绪方法：Redis 无表可建，no-op。"""

    # -- 内部 ---------------------------------------------------------------

    def _hash_to_record(
        self, user_id: str, agent_id: str, session_id: str, data: dict
    ) -> MemorySessionStateRecord:
        return MemorySessionStateRecord(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            turns=int(data.get("turns", 0)),
            cursor_created_at=data.get("cursor_created_at", ""),
            cursor_msg_id=data.get("cursor_msg_id", ""),
            switch_epoch=int(data.get("switch_epoch", 0)),
            last_active_at=float(data.get("last_active_at", 0.0)),
        )

    async def _expire(self, key: str) -> None:
        await self._redis.expire(key, self._state_ttl)

    async def _sync_pending(self, member: str, turns: int, score: float) -> None:
        """维护 pending 副索引的不变量：`turns >= 1` → ZADD，否则 ZREM。

        ``ZADD`` 幂等（已存在则刷新 score），故任何一次漏写都会被后续的心跳 /
        计数自愈；``turns >= 1`` 时同时把 score 刷成最新，使该会话自然离开
        "静默够久"的候选区。
        """
        if turns >= 1:
            await self._redis.zadd(_PENDING_ZSET, {member: score})
        else:
            await self._redis.zrem(_PENDING_ZSET, member)

    # -- agent 级配置--------------------------------------------------

    async def upsert_memory_config(
        self, record: MemoryAgentConfigRecord
    ) -> MemoryAgentConfigRecord:
        key = _config_key(record.agent_id)
        # 身份只在键名里：value 剔除 agent_id（§3.8）
        await self._redis.set(
            key, record.model_dump_json(exclude={"agent_id"})
        )
        await self._redis.sadd(_CONFIGS_SET, record.agent_id)
        return record

    async def get_memory_config(
        self, agent_id: str
    ) -> MemoryAgentConfigRecord | None:
        raw = await self._redis.get(_config_key(agent_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        # value 里没有 agent_id（键名才是真源）→ 读时由入参拼回
        data = json.loads(raw)
        data.update(agent_id=agent_id)
        return MemoryAgentConfigRecord.model_validate(data)

    async def list_memory_configs(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[MemoryAgentConfigRecord]:
        """按 agent_id 升序分页（默认 ``LIMIT 100``，与 SQL 后端语义一致）。"""
        members = await self._redis.smembers(_CONFIGS_SET)
        agent_ids = sorted(
            member.decode("utf-8") if isinstance(member, bytes) else member
            for member in members
        )
        records: list[MemoryAgentConfigRecord] = []
        for agent_id in agent_ids[offset : offset + limit]:
            record = await self.get_memory_config(agent_id)
            if record is not None:  # 悬空索引成员跳过
                records.append(record)
        return records

    async def delete_memory_config(self, agent_id: str) -> bool:
        """SREM 返回值兼存在性反馈，再 DEL 主键。"""
        existed = bool(await self._redis.srem(_CONFIGS_SET, agent_id))
        await self._redis.delete(_config_key(agent_id))
        return existed

    # -- 会话状态 -------------------------------------------------------------

    async def touch_memory_session(
        self, user_id: str, agent_id: str, session_id: str, now: float
    ) -> MemorySessionStateRecord | None:
        """只更新已存在状态的 last_active_at；不存在 → None（不创建）。"""
        key = _session_hash(user_id, agent_id, session_id)
        member = f"{user_id}:{agent_id}:{session_id}"
        if not await self._redis.exists(key):
            return None
        await self._redis.hset(key, "last_active_at", now)
        await self._expire(key)
        await self._redis.zadd(_SESSIONS_ZSET, {member: now})
        data = await self._redis.hgetall(key)
        record = self._hash_to_record(
            user_id, agent_id, session_id,
            {k.decode() if isinstance(k, bytes) else k:
             v.decode() if isinstance(v, bytes) else v
             for k, v in data.items()},
        )
        # 本次心跳已把该会话刷成活跃：pending 的 score 同步刷新（turns>=1 才在）；
        # 返回的 record 本就带 turns，故零额外往返
        await self._sync_pending(member, record.turns, now)
        return record

    async def ensure_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> bool:
        """唯一创建入口：不存在才建档（全字段一次写入）；已存在 → False。"""
        key = _session_hash(user_id, agent_id, session_id)
        member = f"{user_id}:{agent_id}:{session_id}"
        if await self._redis.exists(key):
            return False
        await self._redis.hset(
            key,
            mapping={
                "turns": 0,
                "cursor_created_at": cursor_created_at or "",
                "cursor_msg_id": cursor_msg_id or "",
                "switch_epoch": epoch,
                "last_active_at": now,
            },
        )
        await self._expire(key)
        await self._redis.zadd(_SESSIONS_ZSET, {member: now})
        # 新状态 turns=0：顺手清 pending 残留（Hash 可能刚被 TTL 回收后重建）
        await self._redis.zrem(_PENDING_ZSET, member)
        return True

    async def reset_memory_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        *,
        cursor_created_at: str,
        cursor_msg_id: str,
        epoch: int,
        now: float,
    ) -> MemorySessionStateRecord:
        """无条件重写（upsert）：turns=0 + 新游标 + 新 epoch + 心跳。"""
        key = _session_hash(user_id, agent_id, session_id)
        member = f"{user_id}:{agent_id}:{session_id}"
        await self._redis.hset(
            key,
            mapping={
                "turns": 0,
                "cursor_created_at": cursor_created_at or "",
                "cursor_msg_id": cursor_msg_id or "",
                "switch_epoch": epoch,
                "last_active_at": now,
            },
        )
        await self._expire(key)
        await self._redis.zadd(_SESSIONS_ZSET, {member: now})
        # 重置后 turns=0：同样清 pending 残留
        await self._redis.zrem(_PENDING_ZSET, member)
        return MemorySessionStateRecord(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            turns=0,
            cursor_created_at=cursor_created_at or "",
            cursor_msg_id=cursor_msg_id or "",
            switch_epoch=epoch,
            last_active_at=now,
        )

    async def increment_memory_session_turns(
        self, user_id: str, agent_id: str, session_id: str
    ) -> int:
        """HINCRBY 原子自增；**不存在 → 0（不创建）**；同步刷心跳/EXPIRE/ZADD。"""
        key = _session_hash(user_id, agent_id, session_id)
        member = f"{user_id}:{agent_id}:{session_id}"
        now = time.time()
        if not await self._redis.exists(key):
            return 0
        turns = await self._redis.hincrby(key, "turns", 1)
        await self._redis.hset(key, "last_active_at", now)
        await self._expire(key)
        await self._redis.zadd(_SESSIONS_ZSET, {member: now})
        # 计数 ≥ 1 → 进 pending（判据用 "≥1" 而非 "==1"：ZADD 幂等可自愈）
        await self._sync_pending(member, int(turns), now)
        return int(turns)

    async def get_memory_session_state(
        self, user_id: str, agent_id: str, session_id: str
    ) -> MemorySessionStateRecord | None:
        key = _session_hash(user_id, agent_id, session_id)
        data = await self._redis.hgetall(key)
        if not data:
            return None
        return self._hash_to_record(
            user_id, agent_id, session_id,
            {k.decode() if isinstance(k, bytes) else k:
             v.decode() if isinstance(v, bytes) else v
             for k, v in data.items()},
        )

    async def set_memory_session_cursor(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        created_at: str,
        msg_id: str,
    ) -> None:
        """Hash 不存在时 no-op 记 debug（提取流程先 get 保证存在）。"""
        key = _session_hash(user_id, agent_id, session_id)
        if not await self._redis.exists(key):
            logger.debug("memory: set cursor on missing session %s — no-op", key)
            return
        await self._redis.hset(
            key, mapping={"cursor_created_at": created_at, "cursor_msg_id": msg_id}
        )
        await self._expire(key)

    async def reset_memory_session_turns(
        self, user_id: str, agent_id: str, session_id: str, turns: int = 0
    ) -> None:
        """把 turns 置为 ``turns``（默认 0；cursor 保留）；Hash 不存在时 no-op。"""
        key = _session_hash(user_id, agent_id, session_id)
        if not await self._redis.exists(key):
            logger.debug("memory: reset turns on missing session %s — no-op", key)
            return
        await self._redis.hset(key, "turns", turns)
        await self._expire(key)
        if turns <= 0:
            # 结清 → 出 pending。turns>=1（"未结清"分支）时不动作：它本就
            # ≥1 已在场，且此处**不能**刷 score（否则会把静默已久的会话
            # 伪装成活跃、推迟它的兜底提取）
            await self._redis.zrem(
                _PENDING_ZSET, _member(user_id, agent_id, session_id)
            )

    async def delete_memory_session_state(
        self, user_id: str, agent_id: str, session_id: str
    ) -> bool:
        """DEL Hash + ZREM 两处索引；返回是否有 Hash 被删。"""
        key = _session_hash(user_id, agent_id, session_id)
        member = _member(user_id, agent_id, session_id)
        deleted = bool(await self._redis.delete(key))
        await self._redis.zrem(_SESSIONS_ZSET, member)
        await self._redis.zrem(_PENDING_ZSET, member)
        return deleted

    async def delete_stale_session_states(
        self, last_active_before: float, limit: int
    ) -> int:
        """ZSET 取一批最老候选 → pipeline 清 Hash + ZREM，返回候选数。

        返回 ``len(pairs)`` 而非 ``DEL`` 命中数：Hash 的 TTL 与清理阈值同源
        （都由 ``state_ttl_secs`` 推导），member 跨过超龄线时其 Hash 多半已
        被过期回收 → ``DEL`` 返回 0 是**常态**；若按"删除数"返回，稳态下恒
        为 0 会让调用方第一批就收手、日志也失真。悬空成员的 ZREM 同样是有效
        的清理，故计入候选数。

        阈值端点取排他（``"("`` 前缀）：ZSET 区间默认闭区间，而 SQL 侧是
        ``<``；不加 ``(`` 会让"心跳恰等于阈值"的会话在 Redis 被删、在 SQL
        不被删。
        """
        pairs = await self._redis.zrangebyscore(
            _SESSIONS_ZSET,
            "-inf",
            f"({last_active_before}",
            start=0,
            num=limit,
            withscores=True,
        )
        if not pairs:
            return 0
        async with self._redis.pipeline(transaction=False) as pipe:
            for raw, _score in pairs:
                member = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                try:
                    u, a, s = member.split(":", 2)
                except ValueError:
                    pipe.zrem(_SESSIONS_ZSET, member)  # 非法成员顺手清
                    pipe.zrem(_PENDING_ZSET, member)
                    continue
                pipe.delete(_session_hash(u, a, s))    # Hash 可能已不存在（DEL=0）
                pipe.zrem(_SESSIONS_ZSET, member)      # 索引同步回收
                pipe.zrem(_PENDING_ZSET, member)       # 待提取索引一并清（§3.5.5）
            await pipe.execute()
        return len(pairs)

    async def delete_agent_session_states(self, agent_id: str) -> int:
        """扫完 ``sessions`` 再删，返回处理条数。
        """
        hits: list[str] = []              # 只存 member 字符串（≈14B/个）
        cursor = 0
        while True:                       # 收集阶段：只读 → 集合不缩，游标可靠
            cursor, items = await self._redis.zscan(
                _SESSIONS_ZSET, cursor=cursor, count=_SCAN_PAGE
            )
            for raw, _score in items:
                member = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                try:
                    _u, a, _s = member.split(":", 2)
                except ValueError:
                    hits.append(member)   # 非法 member：顺手回收索引
                    continue
                if a == agent_id:
                    hits.append(member)
            if cursor == 0:               # 全表扫描完成
                break
        if not hits:
            return 0
        for start in range(0, len(hits), _DELETE_CHUNK):   # 删除阶段：此刻才动集合
            async with self._redis.pipeline(transaction=False) as pipe:
                for member in hits[start : start + _DELETE_CHUNK]:
                    try:
                        u, a, s = member.split(":", 2)
                    except ValueError:
                        pipe.zrem(_SESSIONS_ZSET, member)
                        pipe.zrem(_PENDING_ZSET, member)
                        continue
                    pipe.delete(_session_hash(u, a, s))  # Hash 可能已过期（DEL=0）
                    pipe.zrem(_SESSIONS_ZSET, member)    # 主索引
                    pipe.zrem(_PENDING_ZSET, member)     # 待提取索引（pending ⊆ sessions）
                await pipe.execute()
        return len(hits)

    async def list_memory_session_states(
        self,
        *,
        last_active_before: float,
        limit: int | None = None,
    ) -> list[MemorySessionStateRecord]:
        """提取候选：``pending`` 副索引精确命中，不读无关 Hash。"""
        pairs = await self._redis.zrangebyscore(
            _PENDING_ZSET,
            "-inf",
            f"({last_active_before}",
            start=0,
            num=limit,
            withscores=True,
        )
        parsed: list[tuple[str, tuple[str, str, str]]] = []
        stale: list[str] = []             # 非法 member / 悬空 member：只回收索引
        for raw, _score in pairs:
            member = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            try:
                u, a, s = member.split(":", 2)
            except ValueError:
                stale.append(member)
                continue
            parsed.append((member, (u, a, s)))
        records: list[MemorySessionStateRecord] = []
        if parsed:
            async with self._redis.pipeline(transaction=False) as pipe:
                for _item, (u, a, s) in parsed:
                    pipe.hgetall(_session_hash(u, a, s))
                datas = await pipe.execute()
            for (item, (u, a, s)), data in zip(parsed, datas):
                if not data:              # Hash 已过期 → 悬空成员（不返回）
                    stale.append(item)
                    continue
                records.append(
                    self._hash_to_record(
                        u, a, s,
                        {k.decode() if isinstance(k, bytes) else k:
                         v.decode() if isinstance(v, bytes) else v
                         for k, v in data.items()},
                    )
                )
        if stale:
            async with self._redis.pipeline(transaction=False) as pipe:
                for member in stale:
                    pipe.zrem(_PENDING_ZSET, member)
                    pipe.zrem(_SESSIONS_ZSET, member)
                await pipe.execute()
        return records


__all__ = ["RedisMemoryStorage"]
