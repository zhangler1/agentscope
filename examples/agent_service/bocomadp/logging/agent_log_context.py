# -*- coding: utf-8 -*-
"""agent 日志上下文辅助 —— 从 ``agent`` 对象提取识别字段供日志使用。

统一输出 ``session_id / reply_id / agent_id / user_id / run_id`` 五元组
（与 ``event_log.py`` 的事件公共上下文一致），缺失时以 ``-`` 兜底，
不影响日志可用性。``user_id`` 由 ASGI 层（X-User-ID 头）绑定，``run_id``
由路由层在 ``ChatRunRegistry.spawn`` 前绑定（后台任务经 asyncio 复制
上下文继承）；两者缺失时均展示 ``-``。
"""
from __future__ import annotations

from typing import Any

from .trace_context import get_current_run_id, get_current_user_id


def _session_id(agent: Any) -> str:
    return getattr(getattr(agent, "state", None), "session_id", "-")


def _reply_id(agent: Any) -> str:
    """``AgentState.reply_id`` 是 property，本质读 reply_context.reply_id。"""
    state = getattr(agent, "state", None)
    if state is None:
        return "-"
    # 优先用 property，缺失时回退 reply_context.reply_id
    return getattr(state, "reply_id", "-") or "-"


def _agent_id(agent: Any) -> str:
    """框架中 ``Agent.name`` 即 ``agent_id``（事件/消息的 name 字段同源）。"""
    return getattr(agent, "name", "-") or "-"


def ctx_fields(agent: Any) -> str:
    """返回事件公共上下文段：session/reply/agent/user/run 五元组。"""
    return (
        f"session_id={_session_id(agent)} reply_id={_reply_id(agent)} "
        f"agent_id={_agent_id(agent)} user_id={get_current_user_id() or '-'} "
        f"run_id={get_current_run_id() or '-'}"
    )


__all__ = ["ctx_fields", "_session_id", "_reply_id", "_agent_id"]
