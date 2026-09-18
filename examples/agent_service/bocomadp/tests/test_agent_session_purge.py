# -*- coding: utf-8 -*-
"""agent_session_purge 单元测试：删智能体 → 所有用户的会话与消息全量清理。

覆盖：

1. ``purge_agent_sessions``：owner + 他人（使用者）的会话/消息一起删，
   其他智能体的数据不动；
2. 包装层 ``_delete_agent_with_purge``：删除失败（False）时不清理；
3. 删除成功 → 框架原逻辑（fake 只删 owner 的）+ 残留兜底清理链路走通；
4. ``patch_agent_session_purge`` 幂等。

monkeypatch ``_original_delete_agent`` 模拟框架原实现（与
test_expert_team.py 的 session_team_cascade 测试同一手法）。直接用裸
引擎建表造数（AsyncSQLAlchemyStorage 退出 async with 会销毁引擎与
session 工厂，纯函数测试用不上它）。

跑法（仓库根目录）::

    venv\\Scripts\\python.exe -m pytest examples/agent_service/bocomadp/tests/test_agent_session_purge.py -q
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from agentscope.app.storage._sql._tables import MessageRow, SessionRow, _Base

import bocomadp.pool_config as pool_config
from bocomadp import agent_session_purge

_T = datetime(2026, 9, 18, 10, 0, 0)


@pytest.fixture
def engine(tmp_path):
    """裸 sqlite 引擎（建好 sessions/messages 表）+ pool_config 单例替换。"""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'purge.db'}")

    async def _provision() -> None:
        async with eng.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

    asyncio.run(_provision())
    pool_config._engine = eng
    yield eng
    pool_config._engine = None

    async def _dispose() -> None:
        await eng.dispose()

    asyncio.run(_dispose())


def _add_session_and_message(
    engine,
    sid: str,
    user_id: str,
    agent_id: str,
    msg_id: str | None = None,
) -> None:
    """插一条会话（可选带一条消息），模拟"某用户和该智能体聊过天"。"""

    async def _go() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                sa.insert(SessionRow).values(
                    id=sid,
                    user_id=user_id,
                    agent_id=agent_id,
                    source="user",
                    payload={"config": {"name": "会话", "workspace_id": "w"}},
                    created_at=_T,
                    updated_at=_T,
                ),
            )
            if msg_id is not None:
                await conn.execute(
                    sa.insert(MessageRow).values(
                        session_id=sid,
                        msg_id=msg_id,
                        created_at=_T,
                        payload={"role": "user", "content": "你好"},
                    ),
                )

    asyncio.run(_go())


def _count_rows(engine, table: str) -> int:
    async def _go() -> int:
        async with engine.connect() as conn:
            return int(
                (
                    await conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}"))
                ).scalar_one(),
            )

    return asyncio.run(_go())


def _list_ids(engine, sql: str) -> list:
    async def _go() -> list:
        async with engine.connect() as conn:
            return (await conn.execute(sa.text(sql))).scalars().all()

    return asyncio.run(_go())


def test_purge_removes_all_users_sessions(engine):
    """owner + 他人使用者的会话/消息一起清，其他智能体的数据不动。"""
    # 被删智能体 dead-agent：owner 自己的会话 + 别人（alice）的会话
    _add_session_and_message(engine, "s-owner", "u1", "dead-agent", "m1")
    _add_session_and_message(engine, "s-guest", "alice", "dead-agent", "m2")
    # 无消息的会话也顺带覆盖
    _add_session_and_message(engine, "s-guest2", "alice", "dead-agent")
    # 别的智能体的数据不应被波及
    _add_session_and_message(engine, "s-other", "u1", "alive-agent", "m3")

    purged = asyncio.run(
        agent_session_purge.purge_agent_sessions(engine, "dead-agent"),
    )

    assert purged == 3
    # dead-agent 的会话与消息全没了，只剩其他智能体的数据
    assert _list_ids(engine, "SELECT id FROM sessions") == ["s-other"]
    assert _list_ids(engine, "SELECT msg_id FROM messages") == ["m3"]


def test_wrapper_skips_purge_when_delete_fails(engine, monkeypatch):
    """框架原实现返回 False（智能体不存在）→ 不做任何清理。"""

    async def fake_original(self, user_id, agent_id):
        return False

    monkeypatch.setattr(
        agent_session_purge,
        "_original_delete_agent",
        fake_original,
    )
    _add_session_and_message(engine, "s1", "u1", "ghost-agent", "m1")

    result = asyncio.run(
        agent_session_purge._delete_agent_with_purge(
            SimpleNamespace(), "u1", "ghost-agent",
        ),
    )

    assert result is False
    assert _count_rows(engine, "sessions") == 1
    assert _count_rows(engine, "messages") == 1


def test_wrapper_purges_after_successful_delete(engine, monkeypatch):
    """删除成功 → fake 原实现跑完后，残留会话被兜底清掉。"""
    calls: list[tuple[str, str]] = []

    async def fake_original(self, user_id, agent_id):
        calls.append((user_id, agent_id))
        # 模拟框架行为：owner 自己的会话连消息一起删（真框架
        # delete_session 的级联），他人的留着
        async with engine.begin() as conn:
            await conn.execute(
                sa.text(
                    "DELETE FROM messages WHERE session_id IN ("
                    "  SELECT id FROM sessions WHERE agent_id = :aid "
                    "  AND user_id = :uid)",
                ),
                {"aid": agent_id, "uid": user_id},
            )
            await conn.execute(
                sa.text(
                    "DELETE FROM sessions WHERE agent_id = :aid "
                    "AND user_id = :uid",
                ),
                {"aid": agent_id, "uid": user_id},
            )
        return True

    monkeypatch.setattr(
        agent_session_purge,
        "_original_delete_agent",
        fake_original,
    )
    # owner 的会话（fake 会删掉）+ alice 的会话（框架够不到，靠兜底清）
    _add_session_and_message(engine, "s-owner", "u1", "dead-agent", "m1")
    _add_session_and_message(engine, "s-guest", "alice", "dead-agent", "m2")

    result = asyncio.run(
        agent_session_purge._delete_agent_with_purge(
            SimpleNamespace(), "u1", "dead-agent",
        ),
    )

    assert result is True
    assert calls == [("u1", "dead-agent")]
    assert _count_rows(engine, "sessions") == 0
    assert _count_rows(engine, "messages") == 0


def test_patch_is_idempotent(monkeypatch):
    """重复 patch 不重复包装（第二次调用直接返回）。"""
    from agentscope.app._service import _session as _session_module

    original = _session_module.SessionService.delete_agent
    try:
        agent_session_purge.patch_agent_session_purge()
        wrapped = _session_module.SessionService.delete_agent
        assert wrapped is agent_session_purge._delete_agent_with_purge

        agent_session_purge.patch_agent_session_purge()
        assert _session_module.SessionService.delete_agent is wrapped
    finally:
        # 还原类属性与单例标记，防止污染其他测试文件
        _session_module.SessionService.delete_agent = original
        agent_session_purge._original_delete_agent = None
