# -*- coding: utf-8 -*-
"""让资源列表按「最近修改优先」排序 —— 不改框架源码的补丁。

框架的 ``ResourceAccessService.list_resource`` 返回资源的顺序取决于存储层
（SQL 无 ORDER BY；Redis Set 无序），顶部 agent 列表与团队成员列表都会
穿过这个方法。BocomADP 需要「最新修改的排最前」，因此在这里把框架方法
包一层：调用原方法后按 ``updated_at`` 倒序排序。

这段逻辑此前直接写在 ``src/agentscope/app/_service/_access.py`` 里，
现按「框架源码不动、企业逻辑进 bocomadp」的约定搬到这里，行为完全一致。

启动时调用一次（幂等，重复调用无害）：:

    from bocomadp.agent_list_sort import patch_agent_list_sort
    patch_agent_list_sort()
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("bocomadp.agent_list_sort")

_original_list_resource: Any = None


async def _sorted_list_resource(
    self: Any,
    viewer_id: str,
    kind: Any,
    parent_agent_id: str | None = None,
) -> list[Any]:
    """调用框架原方法，随后按 updated_at 倒序（最近修改优先）。"""
    views = await _original_list_resource(self, viewer_id, kind, parent_agent_id)
    views.sort(
        key=lambda v: getattr(v, "updated_at"),
        reverse=True,
    )
    return views


def patch_agent_list_sort() -> None:
    """幂等地把 ``ResourceAccessService.list_resource`` 包上排序逻辑。"""
    global _original_list_resource
    if _original_list_resource is not None:
        return
    from agentscope.app._service._access import ResourceAccessService

    _original_list_resource = ResourceAccessService.list_resource
    ResourceAccessService.list_resource = _sorted_list_resource
    logger.info("patched ResourceAccessService.list_resource -> updated_at desc sort")


__all__ = ["patch_agent_list_sort"]
