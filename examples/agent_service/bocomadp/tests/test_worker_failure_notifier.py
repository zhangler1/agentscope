# -*- coding: utf-8 -*-
"""WorkerFailureNotifier 的唤醒目标测试。

焦点：``enqueue_run_trigger`` 必须携带 **leader 的 agent_id**。
历史缺陷是传了 worker 的 agent_id，而 ``WakeupDispatcher`` 用
``get_session(user_id, agent_id, session_id)`` 校验，三元组不匹配 →
触发被当作孤儿丢弃，HintBlock 滞留在 leader inbox 直到用户下次发言。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from agentscope.event import ReplyEndEvent, ReplyFinishedReason

from bocomadp.projectors import _worker_failure_notifier as mod
from bocomadp.projectors._worker_failure_notifier import WorkerFailureNotifier

LEADER_SID = "leader-sid"
LEADER_AGENT = "leader-1"
WORKER_SID = "worker-sid"
WORKER_AGENT = "worker-1"


class _Storage:
    """最小 storage：只实现 notifier 用到的 get_team / get_session。"""

    def __init__(self, *, leader_exists: bool = True) -> None:
        self.leader_exists = leader_exists

    async def get_team(self, user_id: str, team_id: str):
        return SimpleNamespace(session_id=LEADER_SID)

    async def get_session(self, user_id: str, agent_id: str, session_id: str):
        # 与框架语义一致：agent_id 为空串表示「不限 owner」
        if not self.leader_exists or session_id != LEADER_SID:
            return None
        if agent_id not in ("", LEADER_AGENT):
            return None
        return SimpleNamespace(id=session_id, agent_id=LEADER_AGENT)


class _Bus:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, dict]] = []

    async def queue_push(self, key: str, payload: dict) -> None:
        self.pushed.append((key, payload))

    async def publish(self, key: str, payload: dict) -> None:
        return None


class _Projection:
    def __init__(self) -> None:
        self._bus = _Bus()
        self.upserts: list[tuple] = []
        self.publishes: list[tuple] = []

    async def upsert(self, sid, kind, entry_id, payload) -> None:
        self.upserts.append((sid, kind, entry_id, payload))

    async def publish(self, sid, name, payload) -> None:
        self.publishes.append((sid, name, payload))


def _records():
    session = SimpleNamespace(id=WORKER_SID, team_id="team-1")
    agent = SimpleNamespace(id=WORKER_AGENT, data=SimpleNamespace(name="A"))
    event = ReplyEndEvent(
        session_id=WORKER_SID,
        reply_id="reply-1",
        finished_reason=ReplyFinishedReason.ERROR,
    )
    return session, agent, event


class TestWorkerFailureWakeup(IsolatedAsyncioTestCase):
    async def test_wakeup_uses_leader_agent_id(self) -> None:
        """唤醒必须用 leader 的 agent_id，而不是失败 worker 的。"""
        projection = _Projection()
        called: dict = {}

        async def _fake_enqueue(bus, **kwargs) -> None:
            called.update(kwargs)

        with patch.object(mod, "enqueue_run_trigger", _fake_enqueue):
            await WorkerFailureNotifier(_Storage()).maybe_project(
                "u1",
                *_records(),
                projection,
            )

        self.assertEqual(called["session_id"], LEADER_SID)
        self.assertEqual(called["agent_id"], LEADER_AGENT)
        self.assertEqual(called["user_id"], "u1")

    async def test_missing_leader_session_skips_wakeup(self) -> None:
        """leader 会话不存在时不投唤醒，但 UI 卡片/事件照旧。"""
        projection = _Projection()
        called: dict = {}

        async def _fake_enqueue(bus, **kwargs) -> None:
            called.update(kwargs)

        with patch.object(mod, "enqueue_run_trigger", _fake_enqueue):
            await WorkerFailureNotifier(
                _Storage(leader_exists=False),
            ).maybe_project("u1", *_records(), projection)

        self.assertEqual(called, {})
        self.assertEqual(len(projection.upserts), 1)
        self.assertEqual(len(projection.publishes), 1)

    async def test_completed_worker_is_ignored(self) -> None:
        """正常结束的 worker 不触发任何通知。"""
        projection = _Projection()
        session, agent, _ = _records()
        event = ReplyEndEvent(session_id=WORKER_SID, reply_id="reply-1")

        with patch.object(mod, "enqueue_run_trigger", AsyncMock()) as enqueue:
            await WorkerFailureNotifier(_Storage()).maybe_project(
                "u1",
                session,
                agent,
                event,
                projection,
            )

        enqueue.assert_not_awaited()
        self.assertEqual(projection.upserts, [])
        self.assertEqual(projection.publishes, [])


if __name__ == "__main__":
    import unittest

    unittest.main()
