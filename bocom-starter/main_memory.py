# -*- coding: utf-8 -*-
"""The example script to start the agent service (in-memory message bus).
"""

import os

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope import setup_logger
from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig
from agentscope.permission import PermissionContext, PermissionMode

# -- 行内模型平台（bocom_agentscope 发行版：providers）----------------------
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.credential_model import credential_model_router

# 模型卡目录：本入口文件同级的 models/（随 bocom-starter 交付，whl 内不含）。
EllmChatModel.set_models_dir(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"),
)
setup_logger("INFO")

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

# 主存储：SQLAlchemy 异步 URL（MySQL / OB 兼容 MySQL 协议均可）。
# host 注意：本机直跑用 127.0.0.1（MySQL 容器已把 3306 发布到宿主机）；
# 只有在 compose 网络内部的容器里跑，才用服务名 mysql。
MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"


# create_tables=True 便于首次起服务自动建表（示例/开发用）；生产建议置 False，
# 改为独立步骤执行 alembic upgrade head。pool_pre_ping/pool_recycle 规避服务端
# 回收空闲连接导致的偶发断连。
storage = AsyncSQLAlchemyStorage(
    MYSQL_URL,
    create_tables=True,
    engine_kwargs={
        "pool_pre_ping": True,
        "pool_recycle": 3600,
    },
)

# message_bus 用内存实现（本文件即 memory 版）：状态只在本进程内，不依赖
# 消息中间件；多进程/多副本请改用同目录 main_redis.py 的 RedisMessageBus。
# 与 create_app 共享同一实例（行内模型 key 刷新中间件复用）。
message_bus = InMemoryMessageBus()


# 行内模型平台：ELLM api key 刷新中间件工厂（惰性预刷 + 401 强制刷新重试）。
# refresh_ahead_secs 非必填，默认 300s（key 过期前提前刷新窗口）；如需调整，
# 可给 build_ellm_refresh_middleware(storage, message_bus, refresh_ahead_secs=...)。
_ellm_refresh_factory = build_ellm_refresh_middleware(
    storage,
    message_bus,
)


app = create_app(
    storage=storage,
    message_bus=message_bus,
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
    # Agent 级中间件：只挂行内模型 key 刷新工厂，签名与 create_app 期望一致
    # （async (user_id, agent_id, session_id) -> list[MiddlewareBase]）。
    extra_agent_middlewares=_ellm_refresh_factory,
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
# - /model/credential：凭证配置查询（GET）、凭证部分更新（PATCH）
app.include_router(credential_model_router)


if __name__ == "__main__":
    # Start the service
    uvicorn.run(
        # 模块名必须与文件名一致（本文件是 main_memory.py）
        "main_memory:app",
        host="0.0.0.0",
        port=8000,
        # uvicorn 级别用小写名（与上面 setup_logger 的级别各自独立）
        log_level="INFO",
        # 生产默认不 reload（镜像自包含部署）；本地开发可设 UVICORN_RELOAD=true
        reload=os.getenv("UVICORN_RELOAD", "false").lower()
        in ("1", "true", "yes"),
    )
