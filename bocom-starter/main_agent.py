# -*- coding: utf-8 -*-
"""原生方式创建智能体（非服务模式）—— 终端 Console 交互。

与服务模式（main.py：FastAPI + Web UI，依赖 OceanBase 主存储与 Redis
缓存）相对：不启动服务、不依赖任何存储，直接用 SDK 原生 Agent + 行内
ELLM 模型在终端对话（流式输出、工具调用确认、Ctrl+C 中断均已内置）。

行内大模型平台流程（与服务模式一致）：
按场景码（ELLM_SCENE_CODE）从平台（ELLM_API_KEY_URL）取 api_key，
再携 key 调用平台（ELLM_BASE_URL，OpenAI 兼容端点）。

运行（自动读取同目录 .env）：
    uv run python main_agent.py

环境变量（.env）：
    ELLM_BASE_URL      平台 OpenAI 兼容端点，以 /v1 结尾
    ELLM_SCENE_CODE    场景码（与 ELLM_API_KEY_URL 同配时启动即取 key）
    ELLM_API_KEY_URL   平台取 key 端点（createSceneApiKey.do）
    ELLM_API_KEY       直接指定 api key（不走场景码流程时必填）
    ELLM_MODEL         模型名（默认 deepseek-v4-flash）

本地联调（Mock 大模型平台）：dev_ellm_gateway.py 模拟行内平台——按场景码
签发 DeepSeek key 并把 chat 代理到 api.deepseek.com。先起网关再运行本脚本：

    uv run python dev_ellm_gateway.py        # 0.0.0.0:8001
    uv run python main_agent.py              # .env 已指向 8001

注意：原生模式没有服务模式的 key 刷新中间件（惰性预刷 + 401 重试），
key 过期后需重启脚本重新按场景码获取。
"""
import asyncio
import os

from agentscope.agent import Agent
from agentscope.console import launch_console
from agentscope.tool import Toolkit, Bash, Grep, Glob, Read, Write, Edit

# 行内模型平台（bocom-as 发行版：providers）
from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel
from providers.ellm_key import fetch_ellm_key


def _load_dotenv() -> None:
    """极简 .env 加载（脚本同目录，KEY=VALUE，# 注释）；shell 环境优先。"""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".env",
    )
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


async def main() -> None:
    _load_dotenv()

    scene_code = os.getenv("ELLM_SCENE_CODE")
    api_key_url = os.getenv("ELLM_API_KEY_URL")
    api_key = os.getenv("ELLM_API_KEY", "")

    # 行内流程：按场景码从大模型平台取 api_key（启动时一次）。
    if scene_code and api_key_url:
        api_key, _ttl_ms = fetch_ellm_key(scene_code, api_key_url)
        print(f"[main_agent] scene_code={scene_code} 平台取 key 成功")

    if not api_key:
        raise SystemExit(
            "缺少 api_key：请在 .env 配置 ELLM_API_KEY，或同时配置 "
            "ELLM_SCENE_CODE + ELLM_API_KEY_URL 走平台取 key 流程",
        )

    agent = Agent(
        name="Friday",
        system_prompt="You're a helpful assistant named Friday.",
        model=EllmChatModel(
            credential=ELLMCredential(
                api_key=api_key,
                base_url=os.getenv(
                    "ELLM_BASE_URL",
                    "http://ellm-gateway.example/v1",
                ),
                scene_code=scene_code,
                api_key_url=api_key_url,
            ),
            model=os.getenv("ELLM_MODEL", "deepseek-v4-flash"),
        ),
        toolkit=Toolkit(
            tools=[
                Bash(),
                Grep(),
                Glob(),
                Read(),
                Write(),
                Edit(),
            ],
        ),
    )

    # 终端对话：流式输出、工具调用确认、Ctrl+C 中断均已内置处理
    await launch_console(agent)


if __name__ == "__main__":
    asyncio.run(main())
