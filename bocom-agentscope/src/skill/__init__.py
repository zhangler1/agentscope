# -*- coding: utf-8 -*-
"""行内技能（skill）扩展。

对齐 SDK 侧 ``agentscope.app.hub`` 的 Hub 体系，当前包含：

- :class:`JiaoXinSkillHub`: 交心技能市场技能导出（只实现下载，
  流式返回 :class:`~agentscope.app.hub.SkillArchive`）；
- :func:`list_skills` / :func:`get_skill_content`: 查询与 SKILL.md
  内容获取（返回上游完整响应信封原样内容）；
- :data:`jiaoxin_skillhub_router`: HTTP 路由
  （``POST /skill/jiaoxin/list|content|download``）。
"""
from .jiaoxin_hub import JiaoXinSkillHub
from .routers import jiaoxin_skillhub_router

__all__ = [
    "JiaoXinSkillHub",
    "jiaoxin_skillhub_router",
]
