# -*- coding: utf-8 -*-
"""Per-team watcher for free_handoff worker sessions.

Workflow teams are deliberately excluded: their orchestrator already owns
run completion, stage advancement and final/failed summaries.  For ordinary
``TeamCreate`` teams this watcher turns worker session transitions into
leader-visible status hints.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agentscope.app._bus_ops import enqueue_run_trigger
from agentscope.app._service import SessionService
from agentscope.app.message_bus import MessageBusKeys
from agentscope.message import HintBlock

logger = logging.getLogger("bocomadp.team.watcher")

POLL_SECS = 5.0
START_TIMEOUT_SECS = 30.0


@dataclass
class _WatchState:
    user_id: str
    team_id: str
    leader_session_id: str
    started_at: float = field(default_factory=time.monotonic)
    workers: set[str] = field(default_factory=set)
    seen_running: set[str] = field(default_factory=set)
    last_status: dict[str, str] = field(default_factory=dict)
    baseline_reply: dict[str, str | None] = field(default_factory=dict)
    notified: set[tuple[str, str]] = field(default_factory=set)


class TeamWatcherManager:
    """Own at most one watcher task per runtime team."""

    def __init__(self, storage: Any, message_bus: Any) -> None:
        self._storage = storage
        self._bus = message_bus
        self._sessions = SessionService(storage, message_bus)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, _WatchState] = {}

    async def ensure(
        self,
        *,
        user_id: str,
        team_id: str,
        worker_session_ids: list[str],
    ) -> None:
        if not worker_session_ids:
            return
        state = self._states.get(team_id)
        if state is None:
            team = await self._storage.get_team(user_id, team_id)
            if team is None:
                return
            state = _WatchState(
                user_id=user_id,
                team_id=team_id,
                leader_session_id=team.session_id,
            )
            self._states[team_id] = state
        new_workers = [
            sid for sid in worker_session_ids if sid and sid not in state.workers
        ]
        state.workers.update(new_workers)
        for sid in new_workers:
            reply = await self._last_reply(user_id, sid)
            state.baseline_reply[sid] = getattr(reply, "id", None)
        task = self._tasks.get(team_id)
        if task is None or task.done():
            self._tasks[team_id] = asyncio.create_task(
                self._watch(state),
                name=f"team-watcher:{team_id}",
            )

    async def stop(self, team_id: str) -> None:
        task = self._tasks.pop(team_id, None)
        self._states.pop(team_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _is_notified(self, state: _WatchState, sid: str, key: str) -> bool:
        return (sid, key) in state.notified

    async def _last_reply(self, user_id: str, session_id: str) -> Any:
        try:
            messages, _ = await self._storage.list_messages(
                user_id,
                session_id,
                limit=20,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception("watcher failed to list messages for %s", session_id)
            return None
        for message in reversed(messages):
            if getattr(message, "role", "") == "assistant":
                return message
        return None

    @staticmethod
    def _value(value: Any) -> str:
        return getattr(value, "value", str(value) if value is not None else "missing")

    def _derive(self, status: str, reply: Any) -> tuple[str, str]:
        """Return (notice_key, human status text)."""
        if status == "running":
            return "running", "running"
        if status == "awaiting_permission":
            return "awaiting_permission", "当前等待工具确认"
        if status == "awaiting_external_result":
            return "awaiting_external_result", "当前等待外部结果"
        if status == "missing":
            return "session_lost", "会话丢失"
        if status != "idle":
            return status, f"当前状态：{status}"

        reason = getattr(reply, "finished_reason", None) if reply else None
        reason_value = self._value(reason) if reason is not None else "null"
        error = getattr(reply, "error", None) if reply else None
        error_text = str(error) if error else None
        reply_id = getattr(reply, "id", "unknown") if reply else "unknown"

        if error_text:
            return f"failed:{reply_id}", f"本轮失败，错误：{error_text}"
        if reason_value == "completed":
            return f"completed:{reply_id}", "本轮已结束运行，结果未验证"
        if reason_value == "interrupted":
            return f"interrupted:{reply_id}", "本轮被中断"
        if reason_value in ("error", "exceed_max_iters"):
            return f"failed:{reply_id}", f"本轮异常结束：{reason_value}"
        return f"failed:{reply_id}", "本轮失败：回复未正常结束（finished_reason=null）"

    async def _notify(
        self,
        state: _WatchState,
        *,
        worker_agent_id: str,
        worker_session_id: str,
        key: str,
        text: str,
    ) -> None:
        if self._is_notified(state, worker_session_id, key):
            return
        leader_session = await self._storage.get_session(
            state.user_id,
            "",
            state.leader_session_id,
        )
        if leader_session is None:
            return
        hint = HintBlock(
            hint=(
                f"worker {worker_agent_id}：{text}。\n"
                "这仅表示该 worker 的本轮运行状态，不代表整个团队结束；"
                "请检查结果或决定是否重新派发。"
            ),
            source=json.dumps(
                {"label": "team_worker_status", "sublabel": "watcher"},
                ensure_ascii=False,
            ),
        )
        await self._bus.queue_push(
            MessageBusKeys.inbox(leader_session.id),
            hint.model_dump(mode="json"),
        )
        await enqueue_run_trigger(
            self._bus,
            user_id=state.user_id,
            session_id=leader_session.id,
            agent_id=leader_session.agent_id,
        )
        state.notified.add((worker_session_id, key))

    async def _watch(self, state: _WatchState) -> None:
        try:
            team = await self._storage.get_team(state.user_id, state.team_id)
            if team is None:
                return
            while True:
                team = await self._storage.get_team(state.user_id, state.team_id)
                if team is None or team.session_id != state.leader_session_id:
                    return
                leader_session = await self._storage.get_session(
                    state.user_id,
                    "",
                    state.leader_session_id,
                )
                if leader_session is None:
                    return

                active = False
                for sid in list(state.workers):
                    member = next(
                        (
                            item
                            for item in getattr(team.data, "members", [])
                            if item.session_id == sid
                        ),
                        None,
                    )
                    if member is None:
                        # The member was removed; do not keep watching it.
                        state.workers.discard(sid)
                        continue
                    try:
                        raw_status = await self._sessions.get_session_status(
                            state.user_id,
                            member.agent_id,
                            sid,
                        )
                    except Exception:  # pylint: disable=broad-except
                        logger.exception("watcher status failed for %s", sid)
                        raw_status = None
                    status = (
                        self._value(raw_status)
                        if raw_status is not None
                        else "missing"
                    )
                    state.last_status[sid] = status
                    if status == "running":
                        state.seen_running.add(sid)
                        active = True
                        continue
                    if status in (
                        "awaiting_permission",
                        "awaiting_external_result",
                    ):
                        key, text = self._derive(status, None)
                        await self._notify(
                            state,
                            worker_agent_id=member.agent_id,
                            worker_session_id=sid,
                            key=key,
                            text=text,
                        )
                        active = True
                        continue
                    if status == "idle":
                        reply = await self._last_reply(state.user_id, sid)
                        reply_id = getattr(reply, "id", None)
                        key, text = self._derive(status, reply)
                        # A worker may be idle before its first run starts.
                        # Wait for either a run observation or a new reply;
                        # an old reply from a previous task is a baseline,
                        # not this task's result.
                        if (
                            sid not in state.seen_running
                            and (
                                reply is None
                                or reply_id
                                == state.baseline_reply.get(sid)
                            )
                            and time.monotonic() - state.started_at
                            < START_TIMEOUT_SECS
                        ):
                            active = True
                            continue
                        if (
                            sid not in state.seen_running
                            and (
                                reply is None
                                or reply_id
                                == state.baseline_reply.get(sid)
                            )
                        ):
                            key, text = (
                                "start_timeout",
                                "本轮未观察到运行，可能未启动或会话卡住",
                            )
                        await self._notify(
                            state,
                            worker_agent_id=member.agent_id,
                            worker_session_id=sid,
                            key=key,
                            text=text,
                        )
                        continue
                    if status == "missing":
                        await self._notify(
                            state,
                            worker_agent_id=member.agent_id,
                            worker_session_id=sid,
                            key="session_lost",
                            text="会话丢失",
                        )
                        continue

                if not state.workers:
                    return
                terminal = all(
                    state.last_status.get(sid) not in (
                        "running",
                        "awaiting_permission",
                        "awaiting_external_result",
                        None,
                    )
                    for sid in state.workers
                )
                if not active and terminal:
                    return
                await asyncio.sleep(POLL_SECS)
        except asyncio.CancelledError:
            raise
        except Exception:  # pylint: disable=broad-except
            logger.exception("team watcher failed for %s", state.team_id)
        finally:
            current = self._tasks.get(state.team_id)
            if current is asyncio.current_task():
                self._tasks.pop(state.team_id, None)
                self._states.pop(state.team_id, None)


_manager: TeamWatcherManager | None = None


def _get_manager(storage: Any, message_bus: Any) -> TeamWatcherManager:
    global _manager
    if _manager is None:
        _manager = TeamWatcherManager(storage, message_bus)
    return _manager


async def ensure_team_watcher(
    storage: Any,
    message_bus: Any,
    *,
    user_id: str,
    team_id: str,
    worker_session_ids: list[str],
) -> None:
    await _get_manager(storage, message_bus).ensure(
        user_id=user_id,
        team_id=team_id,
        worker_session_ids=worker_session_ids,
    )


async def stop_team_watcher(team_id: str) -> None:
    if _manager is not None:
        await _manager.stop(team_id)


async def ensure_team_watcher_for_sessions(
    storage: Any,
    message_bus: Any,
    *,
    user_id: str,
    session_ids: list[str],
) -> None:
    """Start/reuse watchers for the team that owns these sessions."""
    team_id: str | None = None
    worker_ids: list[str] = []
    for session_id in dict.fromkeys(session_ids):
        if not session_id:
            continue
        session = await storage.get_session(user_id, "", session_id)
        if session is None or not session.team_id:
            continue
        team_id = team_id or session.team_id
        team = await storage.get_team(user_id, session.team_id)
        if team is not None and session_id != team.session_id:
            worker_ids.append(session_id)
    if team_id and worker_ids:
        await ensure_team_watcher(
            storage,
            message_bus,
            user_id=user_id,
            team_id=team_id,
            worker_session_ids=worker_ids,
        )


__all__ = [
    "TeamWatcherManager",
    "ensure_team_watcher",
    "ensure_team_watcher_for_sessions",
    "stop_team_watcher",
]
