# -*- coding: utf-8 -*-
"""bocom-as SDK 使用示例：Agent 对话 + api_key 中间件自动刷新。
"""

import asyncio
import pathlib
import sys
import traceback
from typing import Any
from urllib.parse import quote

# 直接 `python examples/ellm_sdk_agent_demo.py` 时只有脚本目录进入 sys.path，
# 仓库根不在搜索路径里：这里把 bocom-agentscope/src（providers 顶层包，含
# credential / ellm_chat_model / middleware）与 src（agentscope 源码）补进去，
# 未安装发行版也能直接跑。旧目录名 bocom-as 一并兼容（存在才加入）。
# 必须在下面 import agentscope / providers **之前** 执行。
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

import httpx

from agentscope.agent import Agent
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import Msg, TextBlock

from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import EllmKeyRefreshMiddleware

# ---------------------------------------------------------------------------
# 运行参数（按真实环境修改）
# ---------------------------------------------------------------------------
USER_ID = "test"                 # 凭证 owner（中间件/存取都按此隔离）
GATEWAY_BASE_URL = "https://api.deepseek.com"  # 必填：以 /v1 结尾
SCENE_CODE = "P2024146"                                    # 必填：场景编码
API_KEY_URL = (                                           # 必填：取 key 地址
    "http://127.0.0.1:9100/createSceneApiKey.do"
)
MODEL_NAME = "deepseek-flash"
CONTEXT_SIZE = 1_000_000
SYSTEM_PROMPT = "你是一个乐于助人的助手，请用中文回答。"
# 请求超时：连接/读写分开控制，避免小超时误判网关慢
TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=120.0, pool=10.0)

# 主存储：SQL（MySQL 协议，OB 走 SQLAlchemy 的 mysql 方言）——连接参数不读
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
# 非空时直接用完整 SQLAlchemy 异步 URL 覆盖上面的分项配置
MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"


def build_sql_url() -> str:
    """拼 SQLAlchemy 异步连接 URL（OB / MySQL 通用，走 mysql 方言）。

    用户名/密码必须百分号编码：OB 的登录名是 ``用户@租户``（root@test）
    形式，其中的 ``@`` 不编码会被当成 URL 的 host 分隔符，这里统一用
    ``quote(..., safe="")`` 处理。
    """
    if MYSQL_URL:
        return MYSQL_URL
    login = f"{MYSQL_USER}@{MYSQL_TENANT}" if MYSQL_TENANT else MYSQL_USER
    return (
        f"mysql+aiomysql://{quote(login, safe='')}:"
        f"{quote(MYSQL_PASSWORD, safe='')}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
    )


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def user_message(text: str) -> Msg:
    return Msg(name="user", role="user", content=[TextBlock(text=text)])


async def main() -> None:
    """建 OceanBase(SQL) 存储连接，跑完对话后释放连接池。"""
    # 凭证记录读写走 OB：连接池在 __aenter__ 创建、__aexit__ 关闭，避免退出时
    # 残留连接。create_tables=True 由示例自动建表（生产改由 alembic 管理，置
    # False）；pool_pre_ping/pool_recycle 规避服务端回收空闲连接导致的偶发断连。
    async with AsyncSQLAlchemyStorage(
        build_sql_url(),
        create_tables=True,
        engine_kwargs={
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        },
    ) as storage:
        # 并发防抖锁：单进程用 InMemoryMessageBus；多进程换 RedisMessageBus
        await _conversation(storage, InMemoryMessageBus())


async def _conversation(
    storage: AsyncSQLAlchemyStorage,
    message_bus: Any,
) -> None:
    # 1. 建凭证并写入 storage（key 刷新状态就存这里）
    #    api_key 不填用占位值；中间件首次调用检测到无过期时间戳即触发刷新。
    credential = ELLMCredential(
        base_url=GATEWAY_BASE_URL,
        scene_code=SCENE_CODE,
        api_key_url=API_KEY_URL,
    )
    await storage.upsert_credential(USER_ID, credential)

    # 2. 建模型。
    #    stream=False：Agent 对 ChatResponse / AsyncGenerator 都支持，这里用
    #    非流式可规避 python3.14 + httpcore2 下流式响应在退出时
    #    "generator didn't stop after athrow()" 的关闭告警；
    #    think 注入关闭，避免 <think> 文本干扰 agent。
    model = EllmChatModel(
        credential=credential,
        model=MODEL_NAME,
        context_size=CONTEXT_SIZE,
        stream=False,
        client_kwargs={"timeout": TIMEOUT},  # 显式超时，失败时错误更明确
    )

    # 3. 建 key 刷新中间件并挂到 Agent 上
    #    refresh_ahead_secs 非必填，默认 300s（key 过期前提前刷新）
    refresh_middleware = EllmKeyRefreshMiddleware(
        storage,
        message_bus,
        USER_ID,
    )
    agent = Agent(
        name="assistant",
        system_prompt=SYSTEM_PROMPT,
        model=model,
        middlewares=[refresh_middleware],
    )

    try:
        # 4. 对话：每次模型调用前中间件自动 ensure_fresh_key
        for question in ("你好，介绍一下你自己", "用一句话总结你的能力"):
            print(f"\nuser: {question}")
            reply = await agent.reply(user_message(question))
            print(f"assistant: {reply.get_text_content()}")

        # 5. （可选）查看刷新后的记录状态：apikey_expires_at 已由中间件写回
        stored = await storage.get_credential(USER_ID, credential.id)
        if stored is not None:
            print(
                "\n[key refreshed] apikey_expires_at=%s"
                % stored.data.get("apikey_expires_at")
            )
    finally:
        # 6. 关闭底层 openai client（释放连接池，避免退出时残留异步流）
        await model.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 —— 示例兜底：打印完整报错
        traceback.print_exc()
        print("\n[诊断]")
        print(f"异常类型: {type(exc).__name__}")
        print(f"异常信息: {exc}")
        print(
            "若为连接/超时类错误：请检查 GATEWAY_BASE_URL 是否可达——"
            "host.docker.internal 仅在容器内指向宿主机，本机直跑需换成"
            "实际可访问的网关地址（http://127.0.0.1:端口/v1 等）；"
            "并确认 8001 端口/防火墙与 API key 场景有效。"
        )
        print(
            "若为数据库连接类错误：确认 MYSQL_HOST:MYSQL_PORT（默认 "
            "127.0.0.1:2881）可连通、租户与库已就绪（可先跑 "
            "ob-docker/scripts/init-db.sh 建库）；报 No module named "
            "'aiomysql' 说明驱动没装，执行 pip install "
            "\"sqlalchemy[asyncio]\" aiomysql。"
        )
