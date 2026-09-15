# -*- coding: utf-8 -*-
"""智能体市场操作留痕：**只打控制台日志，不落库**。

管理类操作的留痕统一以 ``[AGENT_MARKET_AUDIT]`` 前缀打印 INFO，直接在
容器控制台查看::

    docker compose logs -f agentscope-service | Select-String AGENT_MARKET_AUDIT

选型理由：
- 少一张表，不必清理/归档历史记录；
- 写库失败不会影响业务流程（日志是同步的、几乎零成本）；
- 运营主要诉求是"谁改了什么"，控制台 + grep 已足够。

代价与后备方案：容器日志有滚动上限，超出后不可回溯。将来若要长期留痕
或做合规审计，接入集中式日志（Loki / ELK）即可，**调用方一行都不用改**
（只需替换本模块的实现）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("bocomadp.market_audit")

# 统一前缀：方便 ``Select-String AGENT_MARKET_AUDIT`` 过滤
_PREFIX = "[AGENT_MARKET_AUDIT]"


def log_audit(
    actor_user_id: str,
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    """打印一条市场管理操作日志。

    Args:
        actor_user_id (`str`): 操作人 user_id。
        action (`str`): 动作标识，如 ``set_agent_tag`` / ``clear_agent_tag`` /
            ``delete_agent_market``。
        target (`str`): 操作对象（智能体 id）。
        detail (`str`): 补充说明（如新标签值）。
    """
    logger.info(
        "%s actor=%s action=%s target=%s detail=%s",
        _PREFIX,
        actor_user_id,
        action,
        target or "-",
        detail or "-",
    )


__all__ = ["log_audit"]
