# -*- coding: utf-8 -*-
"""bocom-as SDK 使用示例：Agent 对话 + api_key 中间件自动刷新。

与 ``ellm_sdk_demo.py``（裸 model 直调）的区别：
- 对话走 :class:`agentscope.agent.Agent`（``await agent.reply(msg)``）；
- 挂载 ``EllmKeyRefreshMiddleware`` 实现自动刷新：每次模型调用前检查
  凭证记录里的 key 是否临近过期（默认提前 300s），过期则向网关
  ``fetch_ellm_key`` 换新 key 并写回；401 ``invalid_api_key`` 时自动
  强刷并重试一次。

自动刷新依赖两个注入对象：
- storage：凭证记录读写，本示例接**真实 Redis 存储**
  （``RedisStorage``，默认 127.0.0.1:6379/0，可用下面的
  ``REDIS_HOST`` / ``REDIS_PORT`` / ``REDIS_DB`` 覆盖）。key 刷新状态
  （``api_key`` / ``apikey_expires_at``）落在 Redis 里，多进程/多实例
  共享同一份 key；按 id 跨 owner 的兜底查询仅 SQL 主存储支持，Redis
  下只按 ``(user_id, credential_id)`` 精确查。
- message_bus：并发防抖锁（单进程用 ``InMemoryMessageBus``，
  多进程用 ``RedisMessageBus``）。

运行前提：
    a) Redis 可连通（默认 127.0.0.1:6379；依赖 ``pip install redis``）；
    b) 已安装 bocom-as 发行版（pip install -e bocom-as）；或未安装时在
       仓库根目录下运行
           PYTHONPATH=bocom-as:bocom-as/src python examples/ellm_sdk_agent_demo.py
"""

import asyncio
import pathlib
import sys
import traceback
from typing import Any

# 直接 `python examples/ellm_sdk_agent_demo.py` 时只有脚本目录进入 sys.path，
# 仓库根不在搜索路径里：这里把 bocom-as（providers / config 顶层包）与
# bocom-as/src（agentscope）补进去，未安装发行版也能直接跑。
# 必须在下面 import agentscope / providers **之前** 执行。
_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (_ROOT / "bocom-as", _ROOT / "bocom-as" / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx

from agentscope.agent import Agent
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import RedisStorage
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

# Redis 存储连接参数（凭证记录读写）
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_PASSWORD: str | None = None


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def user_message(text: str) -> Msg:
    return Msg(name="user", role="user", content=[TextBlock(text=text)])


async def main() -> None:
    """建 Redis 存储连接，跑完对话后释放连接池。"""
    # 凭证记录读写走 Redis：连接池在 __aenter__ 创建、__aexit__ 关闭，
    # 避免退出时残留连接；REDIS_PASSWORD 为 None 时按无密码连接。
    async with RedisStorage(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        password=REDIS_PASSWORD,
    ) as storage:
        # 并发防抖锁：单进程用 InMemoryMessageBus；多进程换 RedisMessageBus
        await _conversation(storage, InMemoryMessageBus())


async def _conversation(storage: RedisStorage, message_bus: Any) -> None:
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
            "若为 Redis 连接类错误：确认 REDIS_HOST:REDIS_PORT 可达、"
            "服务已启动（如 127.0.0.1:6379），需要鉴权时填 REDIS_PASSWORD。"
        )
