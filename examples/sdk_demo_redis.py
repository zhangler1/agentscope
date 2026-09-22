# -*- coding: utf-8 -*-
"""bocom_agentscope SDK 使用示例：Agent 对话 + api_key 中间件自动刷新。

主存储走 SQL，防抖锁用 RedisMessageBus（多进程/多副本共享同一把锁）；
依赖直接装到环境里即可，无需改动 sys.path：
    pip install bocom_agentscope   # 提供 providers；agentscope SDK 由包依赖装上
运行（仍需可达的数据库、Redis 与模型网关）：
    python examples/sdk_demo_redis.py
"""

import asyncio
import os
import traceback

import httpx

from agentscope import setup_logger
from agentscope.agent import Agent
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.message import Msg, TextBlock

from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel
from providers.middleware.ellm_refresh import EllmKeyRefreshMiddleware

# 日志：SDK 与 providers 共用同一个 logger（"as"），这里一次 setup_logger
# 同时控制两者的格式与级别，合法值 INFO/DEBUG/WARNING/ERROR/CRITICAL。
setup_logger("INFO")

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

# 主存储：SQLAlchemy 异步 URL（MySQL / OB 兼容 MySQL 协议均可）；库需先建好
# （create_tables=True 会自动建表）。
MYSQL_URL = "mysql+aiomysql://agentscope:agentscope@127.0.0.1:3306/agentscope"

# 消息总线：RedisMessageBus（并发防抖锁落在 Redis 上，多进程/多副本共享）。
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def user_message(text: str) -> Msg:
    return Msg(name="user", role="user", content=[TextBlock(text=text)])


async def main() -> None:
    """建 SQL 存储 + Redis 消息总线，跑完对话后释放两者。"""
    # 凭证记录读写走 MYSQL_URL：连接池在 __aenter__ 创建、__aexit__ 关闭，避免
    # 退出时残留连接。create_tables=True 由示例自动建表（生产改由 alembic 管理，
    # 置 False）；pool_pre_ping/pool_recycle 规避服务端回收空闲连接导致的偶发断连。
    async with AsyncSQLAlchemyStorage(
        MYSQL_URL,
        create_tables=True,
        engine_kwargs={
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        },
    ) as storage:
        # 并发防抖锁走 Redis：RedisMessageBus 的连接池同样在 __aenter__ 创建、
        # __aexit__ 关闭，故与 storage 一样用 async with 包起来。
        async with RedisMessageBus(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
        ) as message_bus:
            await _conversation(storage, message_bus)


async def _conversation(
    storage: AsyncSQLAlchemyStorage,
    message_bus: RedisMessageBus,
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
            "若为数据库连接类错误：确认 MYSQL_URL 的主机端口（默认 "
            "127.0.0.1:3306）可连通、库已就绪（库需先建好）；"
            "报 No module named 'aiomysql' 说明驱动没装，执行 pip install "
            "\"sqlalchemy[asyncio]\" aiomysql。"
        )
        print(
            "若为 Redis 连接类错误：确认 REDIS_HOST/REDIS_PORT（默认 "
            f"{REDIS_HOST}:{REDIS_PORT}）可达、REDIS_PASSWORD 正确；"
            "报 No module named 'redis' 说明客户端没装，执行 pip install "
            "\"redis[async]\"。"
        )
