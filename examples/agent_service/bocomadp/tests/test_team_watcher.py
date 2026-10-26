# -*- coding: utf-8 -*-
"""Tests for the per-team free_handoff worker watcher."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bocomadp.team import watcher as watcher_mod
from bocomadp.team.watcher import TeamWatcherManager


def _run(coro):
    return asyncio.run(coro)


class _Storage:
    def __init__(self):
        self.team = SimpleNamespace(
            id="t1",
            session_id="leader-session",
            data=SimpleNamespace(
                members=[
                    SimpleNamespace(
                        agent_id="worker-a",
                        session_id="worker-a",
                    ),
                ],
            ),
        )
        self.leader = SimpleNamespace(
            id="leader-session",
            agent_id="leader-1",
        )
        self.reply = SimpleNamespace(
            id="reply-a",
            role="assistant",
            finished_reason="completed",
            error=None,
        )
        self.messages = []

    async def get_team(self, user_id, team_id):
        return self.team

    async def get_session(self, user_id, agent_id, session_id):
        if session_id == "leader-session":
            return self.leader
        return SimpleNamespace(id=session_id, agent_id="worker-a")

    async def list_messages(self, user_id, session_id, limit=20):
        return list(self.messages), False


class _Status:
    def __init__(self, storage):
        self.storage = storage
        self.values = ["running", "idle", "idle", "idle"]

    async def get_session_status(self, user_id, agent_id, session_id):
        value = self.values.pop(0) if len(self.values) > 1 else "idle"
        if value == "idle" and not self.storage.messages:
            self.storage.messages = [self.storage.reply]
        return SimpleNamespace(value=value)


class _Bus:
    def __init__(self):
        self.pushes = []
        self.publishes = []

    async def queue_push(self, key, payload):
        self.pushes.append((key, payload))

    async def publish(self, key, payload):
        self.publishes.append((key, payload))


async def _wait_for_notice(bus: _Bus) -> None:
    for _ in range(30):
        if bus.pushes:
            return
        await asyncio.sleep(0.1)


def test_watcher_notifies_leader_on_worker_completion(monkeypatch):
    monkeypatch.setattr(watcher_mod, "POLL_SECS", 0.05)
    storage = _Storage()
    bus = _Bus()
    manager = TeamWatcherManager(storage, bus)
    manager._sessions = _Status(storage)  # type: ignore[assignment]

    async def scenario():
        await manager.ensure(
            user_id="u1",
            team_id="t1",
            worker_session_ids=["worker-a"],
        )
        await _wait_for_notice(bus)
        await manager.stop("t1")

    _run(scenario())
    assert bus.pushes
    hint_payload = bus.pushes[0][1]
    assert hint_payload["type"] == "hint"
    assert "本轮已结束运行" in hint_payload["hint"]
    assert bus.publishes
