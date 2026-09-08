# -*- coding: utf-8 -*-
"""The example script to start the agent service."""
import os

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.channel import (
    DingTalkChannel,
    DiscordChannel,
    FeishuChannel,
)
from agentscope.app.hub import ClawSkillHub, GitHubMCPHub
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig
from agentscope.middleware import AgenticMemoryMiddleware, MiddlewareBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.rag import ApproxTokenChunker, QdrantStore
from agentscope.workspace import WorkspaceBase

# -- 行内模型平台（bocom-as 发行版：providers）------------------------------
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.credential_model import credential_model_router

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

# 主存储：数据库（AsyncSQLAlchemyStorage；OceanBase 兼容 MySQL 协议，
# 本地/测试可用 MySQL 模拟，切真 OB 只改 DB_URL 地址）。会话/凭证/Agent
# 等业务数据全部落库，与 Redis 无关；create_tables 启动时自动建表
# （全新初始化，dev/单机够用；多副本生产改用 alembic upgrade head）。
storage = AsyncSQLAlchemyStorage(
    url=os.getenv(
        "DB_URL",
        "mysql+aiomysql://agentscope:agentscope@localhost:3306/agentscope",
    ),
    create_tables=True,
    # 连接池健康参数：pre_ping 探测陈旧连接自动重建，recycle 早于
    # 防火墙/NAT 空闲超时回收，避免连接被服务端静默断开
    engine_kwargs={
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    },
)

# 与 create_app 共享同一实例（行内模型 key 刷新中间件复用）。
message_bus = InMemoryMessageBus()

vector_store = QdrantStore(location=":memory:")


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
# refresh_ahead_secs 非必填，默认 300s（key 过期前提前刷新窗口）；如需调整，
# 可给 build_ellm_refresh_middleware(storage, message_bus, refresh_ahead_secs=...)。
_ellm_refresh_factory = build_ellm_refresh_middleware(
    storage,
    message_bus,
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
    # 消息总线：单进程部署 InMemory 即可（主存储已与 Redis 解耦）。
    # 多进程/多副本部署需跨进程总线时，可换 RedisMessageBus（仅瞬时
    # 协调用途：inbox / 唤醒 / 锁；需独立 Redis 实例），取消下方注释
    # 并替换上面的 InMemoryMessageBus()：
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
    # Knowledge base feature — backed by an in-memory Qdrant store. The
    # CollectionPerKbManager allocates one collection per knowledge base,
    # so any embedding dimension is allowed.
    knowledge_base_manager=CollectionPerKbManager(
        storage=storage,
        vector_store=vector_store,
    ),
    # Chunker classes users can pick from when creating a knowledge base;
    # the chosen type and parameters are pinned on the knowledge base.
    knowledge_chunkers=[ApproxTokenChunker],
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
    channels=[
        DingTalkChannel,
        DiscordChannel,
        FeishuChannel,
    ],
)


# 行内模型平台路由：
# - /model/credential：凭证配置查询（GET）、凭证部分更新（PATCH）
app.include_router(credential_model_router)


if __name__ == "__main__":
    # Start the service
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        # 生产默认不 reload（镜像自包含部署）；本地开发可设 UVICORN_RELOAD=true
        reload=os.getenv("UVICORN_RELOAD", "false").lower()
        in ("1", "true", "yes"),
    )
