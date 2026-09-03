# -*- coding: utf-8 -*-
"""The example script to start the agent service."""
import os

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.hub import ClawSkillHub, GitHubMCPHub
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig
from agentscope.middleware import AgenticMemoryMiddleware, MiddlewareBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.workspace import WorkspaceBase

# -- 行内模型平台（bocom-as 发行版：config / providers）--------------------
from config import get_ellm_settings
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.ellm_models import ellm_models_router
from providers.routers.credential_model import credential_model_router
from providers.routers.session_think_tag import session_think_tag_router

default_mcps = [
    MCPClient(
        name="browser-use",
        mcp_config=StdioMCPConfig(
            command="npx",
            args=["@playwright/mcp@latest"],
        ),
        is_stateful=True,
    ),
]

if os.getenv("AMAP_API_KEY"):
    default_mcps.append(
        MCPClient(
            name="amap",
            mcp_config=HttpMCPConfig(
                url=f"https://mcp.amap.com/mcp?key="
                f"{os.environ['AMAP_API_KEY']}",
            ),
            is_stateful=False,
        ),
    )

# 主存储：OceanBase（MySQL 模式，经 aiomysql 驱动）。DATABASE_URL 可整体
# 覆盖；默认连本机 OB（2881 端口，root 空密码，OB CE 容器默认）。
storage = AsyncSQLAlchemyStorage(
    url=os.getenv(
        "DATABASE_URL",
        "mysql+aiomysql://root:@localhost:2881/agentscope?charset=utf8mb4",
    ),
)

# 与 create_app 共享同一实例（行内模型 key 刷新中间件复用）。
message_bus = InMemoryMessageBus()


async def longterm_memory_factory(
    user_id: str,
    agent_id: str,
    session_id: str,
    workspace: WorkspaceBase,
) -> list[MiddlewareBase]:
    """Attach Markdown-file long-term memory, stored under the session's
    workspace so it is reachable through whichever backend is bound."""
    del user_id, agent_id, session_id
    return [
        AgenticMemoryMiddleware(
            workdir=workspace.workdir,
            backend=workspace.get_backend(),
        ),
    ]


# 行内模型平台：ELLM api key 刷新中间件工厂（惰性预刷 + 401 强制刷新重试）。
# 参数取自 bocom-as/config（环境变量 ELLM_* 可覆盖，见 .env）。
_ellm_refresh_factory = build_ellm_refresh_middleware(
    storage,
    message_bus,
    refresh_ahead_secs=get_ellm_settings().refresh_ahead_secs,
)


async def _combined_agent_middlewares(
    user_id: str,
    agent_id: str,
    session_id: str,
    workspace: WorkspaceBase,
) -> list[MiddlewareBase]:
    """合并长程记忆与行内模型 key 刷新中间件（create_app 仅接受单个
    ``extra_agent_middlewares`` 工厂）。"""
    mws = await longterm_memory_factory(
        user_id, agent_id, session_id, workspace,
    )
    mws.extend(await _ellm_refresh_factory(user_id, agent_id, session_id))
    return mws


app = create_app(
    storage=storage,
    message_bus=message_bus,
    # -- To use a Redis-backed message bus instead (recommended for
    # -- multi-process / production deployments), uncomment the lines
    # -- below and replace the InMemoryMessageBus() above:
    #
    # from agentscope.app.message_bus import RedisMessageBus
    # message_bus=RedisMessageBus(
    #     host="localhost",
    #     port=6379,
    # ),
    workspace_manager=LocalWorkspaceManager(
        basedir=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "workspaces",
        ),
        # The default MCP servers that will be added into the workspace
        default_mcps=default_mcps,
    ),
    # Resource hubs the UI browses under /hub. Neither needs credentials
    # of its own — an individual MCP card declares whatever key it wants
    # from the user in its ``inputs_schema``. Passing a ClawHub token
    # only raises the rate limit.
    mcp_hubs=[GitHubMCPHub()],
    skill_hubs=[ClawSkillHub(api_token=os.getenv("CLAWHUB_API_TOKEN"))],
    # Customize your own subagent templates
    custom_subagent_templates=[
        SubAgentTemplate(
            type="explorer",
            description=(
                "Read-only agents specialized in exploration tasks. It can "
                "read files but cannot modify, create, or delete them. Use "
                "this agent type when you need to investigate the codebase, "
                "understand its structure, or gather information from files "
                "to support planning—without making any changes."
            ),
            system_prompt_template="""You are {member_name}, an explorer \
agent in team '{team_name}' led by {leader_name}.

Team purpose: {team_description}

Your role: {member_description}

## Responsibilities
- Complete the exploration tasks assigned by the team leader.
- You are read-only: you may inspect files and the codebase, but you must \
never modify, create, or delete anything.

## Reporting
- Always report the task result back to {leader_name} using the TeamSay \
tool, whether the task succeeds or fails.
- Keep your private reasoning private; only share conclusions and findings \
that the leader needs.

Note: `TeamSay` is your ONLY channel to communicate with {leader_name} and \
the other team members. Any other output you produce is invisible to them, \
so anything you want them to see MUST be sent through `TeamSay`.""",
            permission_context=PermissionContext(
                # Read-only
                mode=PermissionMode.EXPLORE,
            ),
        ),
    ],
    # Long-term memory. The default PER_AGENT workspace isolation makes
    # the memory survive across sessions of the same agent.
    extra_agent_middlewares=_combined_agent_middlewares,
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
)


# 行内模型平台路由：
# - /ellm-models：模型候选管理（GET/POST/PUT/DELETE，Redis 模型表）
# - /ellm-models/session/{session_id}/think-tag：会话级 think-tag 覆盖
#   （优先级：会话级覆盖 > Redis 模型表 > 默认 False）
# - /model/credential：按凭证查候选模型（GET）、凭证部分更新（PATCH）
app.include_router(ellm_models_router)
app.include_router(credential_model_router)
app.include_router(session_think_tag_router)


if __name__ == "__main__":
    # Start the service
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
