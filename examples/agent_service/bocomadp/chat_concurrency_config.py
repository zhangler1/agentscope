# -*- coding: utf-8 -*-
"""Per-agent /chat 并发上限配置：PG 真源 + 进程内缓存 + Redis 热层。

与 ``pool_config.py``（沙箱温池）同范式但键名/表名完全独立——
那张表管 K8s 沙箱预创建数量，本模块管"同时输出中的对话数"，语义不同。

写入路径（管理 API ``PUT /agents/chat-concurrency``）::

    PG UPSERT（持久化真源，重启不丢）
        → 进程内缓存 dict（本地模式的运行时层，闸机热路径读它）
        → Redis HSET（生产模式热层，多实例共享；失败仅降级）

读取路径（闸机热路径 ``ConcurrencyGuard.try_acquire_ex`` /
``MemoryGuard.try_acquire_ex`` 的 ``agent_limit_provider``）::

    Redis 可用：以 Redis 为准（多副本共享的新值）→ 无记录 = 未配置 = 不限
    Redis 不可用：回落进程内缓存 → 无记录 = 不限

Redis 不可用时进入短暂冷却（避免每个请求都白等一次连接超时），冷却结束
后自动重试——Redis 恢复即自动回热，无需重启进程。

启动回填：``load_runtime`` 把 PG 全量刷入进程内缓存与 Redis（Redis 回填失败
仅告警）。

生效上限（``effective_limit``）带审核状态门禁：只有已通过发布审批
（``agent_market`` 名单有行）的智能体才应用其配置值（未配置 = 不限），
其余状态一律硬上限 1；状态查询带 TTL 缓存，失败按宽口径视为已通过。
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from typing import Any

from bocomadp.config import get_app_config

logger = logging.getLogger("bocomadp.chat_concurrency_config")


# ── PG（持久化真源） ──────────────────────────────────────────────

_engine: Any = None


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。"""
    global _engine
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        _engine = create_async_engine(
            get_app_config().db.url,
            pool_pre_ping=True,
        )
        await _ensure_table()
    return _engine


async def _ensure_table() -> None:
    """幂等建表（``agent_chat_concurrency``，含 agent_name 关联名称列）。

    本功能尚未上线部署，无存量表需要迁移；`agent_name` 直接写在建表语句里。
    本地开发若表结构对不上，删掉 ``agent_chat_concurrency`` 表重启即可重建。
    """
    assert _engine is not None
    from sqlalchemy import text

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS agent_chat_concurrency ("
                "agent_id VARCHAR(255) PRIMARY KEY, "
                "max_running INTEGER NOT NULL, "
                "updated_at DOUBLE NOT NULL, "
                "agent_name VARCHAR(255) NOT NULL DEFAULT ''"
                ")",
            ),
        )


async def pg_get_full(agent_id: str) -> tuple[int, float, str] | None:
    """读 PG 配置，返回 ``(max_running, updated_at, agent_name)``；无记录 None。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT max_running, updated_at, agent_name "
                        "FROM agent_chat_concurrency "
                        "WHERE agent_id = :agent_id",
                    ),
                    {"agent_id": agent_id},
                )
            ).first()
        if row is None:
            return None
        return (int(row[0]), float(row[1]), str(row[2] or ""))
    except Exception as e:  # noqa: BLE001 — DB 不可用不影响运行时
        logger.warning("chat_concurrency_config: pg_get_full(%s) failed: %s", agent_id, e)
        return None


async def pg_upsert(
    agent_id: str,
    max_running: int,
    agent_name: str = "",
) -> None:
    """UPSERT 并发上限配置到 PG（真源）；``agent_name`` 为关联智能体名称快照。"""
    engine = await _get_engine()
    from sqlalchemy import text

    # ON CONFLICT 是 PG/sqlite 方言；MySQL 用 ON DUPLICATE KEY UPDATE。
    # 两种方言都把 agent_name 一并更新，保证列表按名称模糊查询始终有值。
    if engine.dialect.name == "mysql":
        upsert = (
            "ON DUPLICATE KEY UPDATE "
            "max_running = VALUES(max_running), "
            "updated_at = VALUES(updated_at), "
            "agent_name = VALUES(agent_name)"
        )
    else:
        upsert = (
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "max_running = :size, updated_at = :ts, agent_name = :name"
        )

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO agent_chat_concurrency "
                "(agent_id, max_running, updated_at, agent_name) "
                "VALUES (:agent_id, :size, :ts, :name) " + upsert,
            ),
            {
                "agent_id": agent_id,
                "size": max_running,
                "ts": time.time(),
                "name": agent_name,
            },
        )


async def pg_delete(agent_id: str) -> None:
    """删除 PG 中的配置（恢复不限）。"""
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM agent_chat_concurrency WHERE agent_id = :agent_id",
            ),
            {"agent_id": agent_id},
        )


async def pg_list_all() -> dict[str, tuple[int, float, str]]:
    """PG 全量配置（启动回填用）：``{agent_id: (size, ts, name)}``。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT agent_id, max_running, updated_at, agent_name "
                        "FROM agent_chat_concurrency",
                    ),
                )
            ).all()
        return {
            str(r[0]): (int(r[1]), float(r[2]), str(r[3] or "")) for r in rows
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("chat_concurrency_config: pg_list_all failed: %s", e)
        return {}


async def pg_list_filtered(
    agent_name: str | None,
    page_num: int,
    page_size: int,
) -> tuple[list[tuple[str, int, float, str]], int]:
    """按智能体名称模糊查询 + 分页（列表接口用）。

    返回 ``(rows, total)``：``rows`` 为当前页
    ``(agent_id, max_running, updated_at, agent_name)``；``total`` 为模糊过滤后
    的总条数（不是本页条数）。``agent_name`` 为空 = 不过滤；LIKE 大小写是否敏感
    取决于库排序规则（MySQL 默认不区分，够用）。
    """
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        where = ""
        params: dict[str, Any] = {}
        if agent_name:
            where = "WHERE agent_name LIKE :kw"
            params["kw"] = f"%{agent_name}%"
        async with engine.connect() as conn:
            total = int(
                (
                    await conn.execute(
                        text(
                            f"SELECT COUNT(*) FROM agent_chat_concurrency {where}",
                        ),
                        params,
                    )
                ).scalar()
                or 0,
            )
            offset = (page_num - 1) * page_size
            rows = (
                await conn.execute(
                    text(
                        f"SELECT agent_id, max_running, updated_at, agent_name "
                        f"FROM agent_chat_concurrency {where} "
                        f"ORDER BY updated_at DESC, agent_id ASC "
                        f"LIMIT :limit OFFSET :offset",
                    ),
                    {**params, "limit": page_size, "offset": offset},
                )
            ).all()
        return [
            (str(r[0]), int(r[1]), float(r[2]), str(r[3] or "")) for r in rows
        ], total
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "chat_concurrency_config: pg_list_filtered failed: %s", e,
        )
        return [], 0


async def pg_set_name(agent_id: str, agent_name: str) -> None:
    """仅补写名称（存量数据回填用，不触碰 updated_at / max_running）。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE agent_chat_concurrency "
                    "SET agent_name = :name WHERE agent_id = :agent_id",
                ),
                {"agent_id": agent_id, "name": agent_name},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "chat_concurrency_config: pg_set_name(%s) failed: %s", agent_id, e,
        )


async def pg_agent_names(agent_ids: list[str]) -> dict[str, str]:
    """批量取智能体名称（``agents`` 表 ``payload`` JSON 里的 ``name``）。

    列表/详情接口展示用；payload 因驱动差异可能是 str 或 dict，两种都接。
    查不到的 id 不出现在返回里。
    """
    if not agent_ids:
        return {}
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        placeholders = ", ".join(f":id{i}" for i in range(len(agent_ids)))
        params = {f"id{i}": aid for i, aid in enumerate(agent_ids)}
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT id, payload FROM agents "
                        f"WHERE id IN ({placeholders})",
                    ),
                    params,
                )
            ).all()
        names: dict[str, str] = {}
        for r in rows:
            payload = r[1]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    payload = {}
            record = payload if isinstance(payload, dict) else {}
            # 框架序列化把记录包在 "data" 下（payload = {"data": {...}}），
            # 名称在里层；顶层直挂 name 的结构一并兼容。
            inner = record.get("data")
            if isinstance(inner, dict):
                record = inner
            name = record.get("name")
            names[str(r[0])] = str(name) if name is not None else ""
        return names
    except Exception as e:  # noqa: BLE001 — 名称附带失败不阻断配置查询
        logger.warning("chat_concurrency_config: pg_agent_names failed: %s", e)
        return {}


# ── 运行时热层（进程内缓存 + Redis，双轨） ────────────────────────

# 进程内缓存：Redis 不可用时的降级副本（限流继续生效，只是副本间不共享）；
# 闸机热路径直读，零网络开销。
_cache: dict[str, int] = {}
# Redis 故障冷却：失败后短期内不再重试（避免每个请求都白等一次连接超时），
# 冷却结束后自动再试——Redis 恢复即自动回热，无需重启进程。
_REDIS_COOLDOWN_SECS = 5.0
_redis_unavailable_until: float = 0.0
_redis: Any = None

# 哨兵：本次 Redis 不可用（区别于"Redis 可用但无记录" = None）
_UNAVAILABLE = object()


def _config_key(agent_id: str) -> str:
    return f"agentscope:chat_concurrency:{agent_id}"


async def _get_redis() -> Any:
    """懒加载长连接（连接参数来自 AppConfig）。"""
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        redis_cfg = get_app_config().redis
        _redis = aioredis.Redis(
            host=redis_cfg.host,
            port=redis_cfg.port,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis


def _redis_cooling_down() -> bool:
    """是否处于 Redis 故障冷却期（冷却期内直接用进程内缓存，不去打网络）。"""
    return time.monotonic() < _redis_unavailable_until


def _mark_redis_down(exc: Exception) -> None:
    """标记 Redis 不可用并进入冷却（冷却结束后自动重试，Redis 恢复即回热）。"""
    global _redis_unavailable_until
    _redis_unavailable_until = time.monotonic() + _REDIS_COOLDOWN_SECS
    logger.warning(
        "chat_concurrency_config: redis unavailable (%s); "
        "use in-process cache for %.0fs",
        exc,
        _REDIS_COOLDOWN_SECS,
    )


async def _redis_hget(agent_id: str) -> Any:
    """读 Redis 中的限额，返回三态：

    - 整数：限额值
    - ``None``：Redis 可用但无记录（未配置）
    - ``_UNAVAILABLE``：Redis 本次不可用（调用方回落进程内缓存）
    """
    if _redis_cooling_down():
        return _UNAVAILABLE
    try:
        r = await _get_redis()
        val = await r.hget(_config_key(agent_id), "max_running")
        return int(val) if val is not None else None
    except Exception as e:  # noqa: BLE001 — Redis 故障降级到进程内缓存
        _mark_redis_down(e)
        return _UNAVAILABLE


async def _redis_hset(agent_id: str, max_running: int) -> bool:
    """写 Redis 中的限额；失败返回 False（不抛，调用方降级）。"""
    if _redis_cooling_down():
        return False
    try:
        r = await _get_redis()
        await r.hset(_config_key(agent_id), "max_running", str(int(max_running)))
        return True
    except Exception as e:  # noqa: BLE001 — PG 已持久化，Redis 失败仅降级
        _mark_redis_down(e)
        return False


async def _redis_hdel(agent_id: str) -> bool:
    """删 Redis 中的限额；失败返回 False（不抛）。"""
    if _redis_cooling_down():
        return False
    try:
        r = await _get_redis()
        await r.hdel(_config_key(agent_id), "max_running")
        return True
    except Exception as e:  # noqa: BLE001
        _mark_redis_down(e)
        return False


async def set_runtime(agent_id: str, max_running: int) -> bool:
    """写运行时层：进程内缓存必写；Redis 尽力而为（失败仅降级，PG 已持久化）。

    返回 Redis 是否同步成功（前端据此提示"热层未同步"）。
    """
    _cache[agent_id] = int(max_running)
    return await _redis_hset(agent_id, max_running)


async def delete_runtime(agent_id: str) -> None:
    """删运行时层配置（恢复不限）。"""
    _cache.pop(agent_id, None)
    await _redis_hdel(agent_id)


async def runtime_get(agent_id: str) -> int | None:
    """闸机热路径读配置；无记录 = ``None`` = 不限。

    Redis 可用时以 Redis 为准（多副本共享的新值）；Redis 不可用时回落
    进程内缓存（限流继续生效，只是副本间不共享），缓存也没有则不限。
    """
    val = await _redis_hget(agent_id)
    if val is _UNAVAILABLE:
        return _cache.get(agent_id)
    return val


# ── 审核状态门禁（agent_market 名单 + 有上限 TTL 缓存） ───────────────

# 状态缓存保质期（秒）：审核状态变更最多延迟一个 TTL 生效，
# 换来闸机热路径几乎零查库。
_APPROVED_TTL_SECS = 30.0
# 缓存上限：智能体平台智能体会很多，用有上限的 TTL 缓存，
# TTL 过期 + 超上限按 LRU 淘汰，避免长生命周期进程里字典无限增长。
_APPROVED_MAXSIZE = 50000


class _ApprovedCache:
    """agent_id → 是否过审 的有上限 TTL 便签（纯内存、每副本独立）。

    与配置热层无关：这里只缓存“是否过审”，真源是共享库 ``agent_market``，
    各副本各自懒加载，有效期内复用，过期或超上限后回源重查。
    """

    def __init__(self, maxsize: int, ttl: float) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._data: "OrderedDict[str, tuple[bool, float]]" = OrderedDict()

    def get(self, agent_id: str) -> bool | None:
        """命中且在有效期内返回值；未命中/过期返回 None。"""
        item = self._data.get(agent_id)
        if item is None:
            return None
        approved, expire = item
        if expire <= time.monotonic():
            del self._data[agent_id]
            return None
        self._data.move_to_end(agent_id)
        return approved

    def set(self, agent_id: str, approved: bool) -> None:
        """写入并刷新 LRU 位置；超上限淘汰最久未用者。"""
        self._data[agent_id] = (approved, time.monotonic() + self._ttl)
        self._data.move_to_end(agent_id)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


_approved_cache = _ApprovedCache(_APPROVED_MAXSIZE, _APPROVED_TTL_SECS)


async def pg_agent_on_market(agent_id: str) -> bool:
    """查 ``agent_market`` 名单是否有该智能体的行（= 已通过发布审批）。

    审批通过的唯一残留凭证：approve 时插入名单行，unpublish 时删除，
    状态自动回落。查询失败向上抛异常，由 ``is_agent_approved`` 统一兜底。
    """
    engine = await _get_engine()
    from sqlalchemy import text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM agent_market "
                    "WHERE agent_id = :agent_id LIMIT 1",
                ),
                {"agent_id": agent_id},
            )
        ).first()
    return row is not None


async def is_agent_approved(agent_id: str) -> bool:
    """智能体是否已通过发布审批（``agent_market`` 名单有行）。

    结果带 TTL 缓存（有上限，详见 ``_ApprovedCache``）：有效期内直接读进程内
    便签，零查库；过期或淘汰后重新查询。查询失败按 fail-open 宽口径处理——
    视为已通过（走原配置逻辑），绝不因状态查询异常阻断正常对话。
    """
    hit = _approved_cache.get(agent_id)
    if hit is not None:
        return hit
    try:
        approved = await pg_agent_on_market(agent_id)
    except Exception as e:  # noqa: BLE001 — 宽口径：查不到状态不当拦路虎
        logger.warning(
            "chat_concurrency_config: is_agent_approved(%s) failed, "
            "treat as approved: %s",
            agent_id, e,
        )
        approved = True
    _approved_cache.set(agent_id, approved)
    return approved


async def effective_limit(agent_id: str) -> int | None:
    """闸机 agent 维度的生效上限（含审核状态门禁）。

    - 已通过审批：应用配置值（未配置 = ``None`` = 不限）；
    - 未通过（not_submitted / pending / rejected）：硬上限 1。
    """
    if not await is_agent_approved(agent_id):
        return 1
    return await runtime_get(agent_id)


async def get_effective(agent_id: str) -> tuple[int | None, str]:
    """接口查询用生效值解析链，返回 ``(value, source)``。

    Redis 有值 →（"redis"）；否则进程内缓存 / PG 真源 →（"pg"）；
    都没有 →（"default"，不限）。
    """
    val = await _redis_hget(agent_id)
    if val is not _UNAVAILABLE and val is not None:
        return int(val), "redis"
    if agent_id in _cache:
        return _cache[agent_id], "pg"
    full = await pg_get_full(agent_id)
    if full is not None:
        return full[0], "pg"
    return None, "default"


async def load_runtime() -> None:
    """启动回填：PG 全量 → 进程内缓存；再尽力回填 Redis。

    缓存始终回填（闸机热路径零网络开销读它）；Redis 回填失败仅告警，
    Redis 恢复后由热路径自动重试并回热。失败不阻断启动。
    """
    try:
        configs = await pg_list_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("chat_concurrency_config: load_runtime pg failed: %s", e)
        return
    _cache.clear()
    for agent_id, (size, _ts, _name) in configs.items():
        _cache[agent_id] = size
    logger.info(
        "chat_concurrency configs loaded: %d (in-process cache ready)", len(configs),
    )
    # 存量行名称回填：老库无 agent_name 列，迁移后默认空串；按 id 关联
    # agents 表补一次，保证列表按名称模糊查询对历史配置也生效。
    empty_ids = [aid for aid, (_s, _t, nm) in configs.items() if not nm]
    if empty_ids:
        fresh = await pg_agent_names(empty_ids)
        for aid, nm in fresh.items():
            if nm:
                await pg_set_name(aid, nm)
    for agent_id, (size, _ts, _name) in configs.items():
        if not await _redis_hset(agent_id, size):
            # 已进入冷却，本轮不再逐条重试；Redis 恢复后热路径自动回热
            break


__all__ = [
    "pg_get_full",
    "pg_upsert",
    "pg_delete",
    "pg_list_all",
    "pg_list_filtered",
    "pg_set_name",
    "pg_agent_names",
    "set_runtime",
    "delete_runtime",
    "runtime_get",
    "pg_agent_on_market",
    "is_agent_approved",
    "effective_limit",
    "get_effective",
    "load_runtime",
]
