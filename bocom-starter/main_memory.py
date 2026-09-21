# -*- coding: utf-8 -*-
"""The example script to start the agent service."""
import os
import pathlib
import sys
from urllib.parse import quote

# 直接 `python main_memory.py` 时只有脚本目录进入 sys.path，仓库根不在搜索路径：
# 这里把 providers（bocom-agentscope/src，含 credential / middleware / routers）
# 与 agentscope 源码（src）补进去，未安装发行版也能直接跑。旧目录名 bocom-as
# 一并兼容（存在才加入）。必须在下面 import agentscope / providers **之前** 执行。
_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (
    _ROOT / "bocom-as",  # 兼容旧目录名
    _ROOT / "bocom-as" / "src",
    _ROOT / "bocom-agentscope",
    _ROOT / "bocom-agentscope" / "src",  # providers
    _ROOT / "src",  # agentscope（未 pip 安装时）
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import uvicorn
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware

from agentscope.app import create_app, SubAgentTemplate
from agentscope.app.hub import ClawSkillHub, GitHubMCPHub
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.rag import ApproxTokenChunker, QdrantStore

# -- 行内模型平台（bocom-as 发行版：providers）------------------------------
from providers.credential import ELLMCredential  # noqa: F401 — 导入即注册
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import build_ellm_refresh_middleware
from providers.routers.credential_model import credential_model_router

# 模型卡目录：本入口文件同级的 models/。以 __file__ 为基准而非 cwd，保证
# python main_memory.py / uvicorn main:app / uvicorn main_memory:app 三种启动
# 方式解析一致；相对路径会按 cwd 解析，故这里显式给出绝对路径。目录缺失时
# set_models_dir 直接抛 FileNotFoundError 禁止启动——模型卡是交付物的一部分，
# 缺了不能退化成"候选列表为空 + 上下文窗口悄悄变小"的静默故障。
EllmChatModel.set_models_dir("./models")

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

# 主存储：SQL（MySQL 协议，OB 走 SQLAlchemy 的 mysql 方言）。连接参数不读
# 环境变量，按真实环境直接改下面这组常量。当前指向本机 OceanBase（MySQL 模式）
# 开发实例：127.0.0.1:2881、租户 test、库 agentscope（见仓库根 ob-docker/，
# 库需先建：ob-docker/scripts/init-db.sh）。
# 接普通 MySQL / MariaDB：MYSQL_PORT 改 3306、MYSQL_TENANT 置空即可。
MYSQL_HOST = "127.0.0.1"
MYSQL_PORT = 2881
MYSQL_USER = "root"
MYSQL_TENANT = "test"  # OB 业务租户；普通 MySQL 留空
MYSQL_PASSWORD = "ObDev_1234"
MYSQL_DATABASE = "agentscope"
# 非空时直接用完整 SQLAlchemy 异步 URL 覆盖上面的分项配置。
# host 注意：本机直跑用 127.0.0.1（MySQL 容器已把 3306 发布到宿主机）；
# 只有在 compose 网络内部的容器里跑，才用服务名 mysql。
MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"


def build_sql_url() -> str:
    """拼 SQLAlchemy 异步连接 URL（OB / MySQL 通用，走 mysql 方言）。

    OB 的登录名是「用户@租户」（如 root@test），其中的 ``@`` 不编码会被
    当成 URL 的 host 分隔符，所以统一做百分号编码。
    """
    if MYSQL_URL:
        return MYSQL_URL
    login = f"{MYSQL_USER}@{MYSQL_TENANT}" if MYSQL_TENANT else MYSQL_USER
    return (
        f"mysql+aiomysql://{quote(login, safe='')}:"
        f"{quote(MYSQL_PASSWORD, safe='')}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
    )


# create_tables=True 便于首次起服务自动建表（示例/开发用）；生产建议置 False，
# 改为独立步骤执行 alembic upgrade head。pool_pre_ping/pool_recycle 规避服务端
# 回收空闲连接导致的偶发断连。
storage = AsyncSQLAlchemyStorage(
    build_sql_url(),
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

vector_store = QdrantStore(location=":memory:")


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
    # message_bus 固定传上面的内存实例（InMemoryMessageBus）；需要 Redis 版
    # 消息总线（多进程 / 多副本部署）请用同目录的 main_redis.py。
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
    # 本 SDK（2.0.5）只接受单个共享 chunker；不传时默认即
    # ApproxTokenChunker()，这里显式写出便于后续按需换参数。
    knowledge_chunker=ApproxTokenChunker(),
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
        port=9000,
        # 生产默认不 reload（镜像自包含部署）；本地开发可设 UVICORN_RELOAD=true
        reload=os.getenv("UVICORN_RELOAD", "false").lower()
        in ("1", "true", "yes"),
    )
