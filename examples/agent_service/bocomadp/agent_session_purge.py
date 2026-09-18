# -*- coding: utf-8 -*-
"""智能体删除时的**全量**使用痕迹清理（覆盖框架"只删 owner 视角"的级联）。

背景与口径
----------
框架 ``SessionService.delete_agent`` 的级联以 **owner 视角**展开：
``storage.list_sessions(owner_id, agent_id)`` 只能看到 owner 自己的
会话。本项目的业务形态是"开放对话"——``sessions.user_id`` 记录的是
**使用者**，市场/他人的智能体被别的用户聊过后，会话行挂在使用者
名下，框架级联够不到，删除后会留孤儿会话（``usage/agents`` 里以
空名冒出来）。

产品口径（2026-09-18 定）：**智能体删除 = 它的全部使用痕迹消失**，
所有用户的会话与消息一并清理。

实现：包装 ``SessionService.delete_agent``（与 ``session_team_cascade``
同一手法：class 级替换、幂等、两个包装链式生效）。原实现先跑完
（owner 的会话已逐个删过——含运行中对话取消与总线清理、agent 行
已删），再用裸 SQL 兜底清掉**残留的**他人会话行与消息行；只删
``agent_id`` 匹配的行，不碰其他智能体的数据。

边界说明：删除瞬间其他用户若有**进行中的对话**，其后的消息落库会经
``upsert_session`` 把会话行重建出来（单行孤儿）；``usage/agents``
的 ``EXISTS`` 过滤（agents 行已删）会把它挡在清单外——查询侧兜底
与删除侧清理互为双保险。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.agent_session_purge")

#: 被包装前的原版 ``SessionService.delete_agent``（幂等标记，非 None
#: 表示已 patch 过）。
_original_delete_agent: Any = None


async def purge_agent_sessions(engine: Any, agent_id: str) -> int:
    """删掉某智能体**所有用户**的残留会话行与消息行。

    messages 先删（按 ``session_id`` 子查询圈定），sessions 后删；
    两条语句在同一个事务里执行。owner 自己的会话已被框架级联逐个
    删过（含取消运行中对话、清总线状态），这里通常只会命中非 owner
    用户（或异常残留）的行。

    Args:
        engine (`Any`): 共享异步引擎（``pool_config._get_engine()``）。
        agent_id (`str`): 被删除的智能体 id。

    Returns:
        `int`: 清理掉的会话行数（消息行数写入日志）。
    """
    from sqlalchemy import text

    async with engine.begin() as conn:
        msg_result = await conn.execute(
            text(
                "DELETE FROM messages "
                "WHERE session_id IN ("
                "  SELECT id FROM sessions WHERE agent_id = :agent_id)"
            ),
            {"agent_id": agent_id},
        )
        sess_result = await conn.execute(
            text("DELETE FROM sessions WHERE agent_id = :agent_id"),
            {"agent_id": agent_id},
        )
    purged = sess_result.rowcount or 0
    if purged or (msg_result.rowcount or 0):
        logger.info(
            "purged residual usage of deleted agent %s: "
            "%d session(s), %d message(s)",
            agent_id,
            purged,
            msg_result.rowcount or 0,
        )
    return purged


async def _delete_agent_with_purge(
    self: Any,
    user_id: str,
    agent_id: str,
) -> bool:
    """先跑框架删除（owner 级联），再全量清理残留会话与消息。"""
    deleted = await _original_delete_agent(self, user_id, agent_id)
    if not deleted:
        return False

    from bocomadp.pool_config import _get_engine

    engine = await _get_engine()
    await purge_agent_sessions(engine, agent_id)
    return True


def patch_agent_session_purge() -> None:
    """Wrap ``SessionService.delete_agent``（幂等，class 级替换）。

    必须在第一次智能体删除前挂载；包装绑在类上，之后任何实例都生效。
    与 ``patch_session_team_cascade`` 链式叠加：谁后挂谁是外层，
    执行顺序不影响最终效果。
    """
    global _original_delete_agent
    if _original_delete_agent is not None:
        return

    from agentscope.app._service import _session as _session_module

    _original_delete_agent = _session_module.SessionService.delete_agent
    _session_module.SessionService.delete_agent = _delete_agent_with_purge
    logger.info(
        "patched %s.delete_agent with full usage purge",
        _session_module.SessionService.__name__,
    )


__all__ = ["patch_agent_session_purge", "purge_agent_sessions"]
