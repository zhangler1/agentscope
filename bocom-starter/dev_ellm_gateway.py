# -*- coding: utf-8 -*-
"""本地开发用 Mock ELLM 大模型平台（OpenAI 兼容，DeepSeek 后端）。

模拟行内"按场景码签发 api_key → 携 key 调用平台"的完整链路：

- ``POST /v1/keys`` —— 模拟行内 ``createSceneApiKey.do``（表单
  ``REQ_MESSAGE`` + ``TRAN_SUCCESS`` 响应）：按场景码签发 api_key。
  签发的 key 即真实 DeepSeek API key（``DEEPSEEK_API_KEY``，自动读取
  同目录 ``.env``），本地即可联调真实模型；
- ``POST /v1/chat/completions`` —— OpenAI 兼容（stream / non-stream），
  反向代理到 DeepSeek（透传 Authorization 与请求体；模型名透传，行内
  模型名 deepseek-v4-flash 与 DeepSeek 直接对齐）。

启动（DEEPSEEK_API_KEY 已在 .env 中，可直接运行）::

    uv run python dev_ellm_gateway.py        # uvicorn 0.0.0.0:8001

配套（bocom-starter/.env，main_agent.py / 服务模式凭证共用）::

    ELLM_BASE_URL=http://localhost:8001/v1
    ELLM_API_KEY_URL=http://localhost:8001/v1/keys
    ELLM_SCENE_CODE=P2024146

Docker 容器内访问宿主机网关：``http://host.docker.internal:8001/v1``
（Linux 主机需在 compose agentscope 服务加
``extra_hosts: ["host.docker.internal:host-gateway"]``）。
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock ELLM Gateway (DeepSeek)")

_DEEPSEEK_CHAT_URL = "https://api.deepseek.com/v1/chat/completions"
# 模拟行内签发 key 的有效期（毫秒）。
_ISSUED_TTL_MS = 1_500_000
_TIMEOUT = httpx.Timeout(connect=10, read=180, write=60, pool=10)


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


def _deepseek_key() -> str:
    """签发给调用方的 api_key：即 DeepSeek key（DEEPSEEK_API_KEY）。"""
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未设置 —— mock 平台签发的 key 即 DeepSeek key",
        )
    return key


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI 兼容 chat completions：反向代理到 DeepSeek，流式与非流式均支持。"""
    body = await request.json()
    auth = request.headers.get("authorization", "")
    stream = body.get("stream", False)

    if not stream:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                _DEEPSEEK_CHAT_URL,
                json=body,
                headers={"Authorization": auth},
            )
        return JSONResponse(r.json(), status_code=r.status_code)

    async def _proxy() -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "POST",
                _DEEPSEEK_CHAT_URL,
                json=body,
                headers={"Authorization": auth},
            ) as r:
                async for chunk in r.aiter_bytes():
                    yield chunk

    return StreamingResponse(
        _proxy(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/keys")
async def create_scene_api_key(REQ_MESSAGE: str = Form(...)) -> Any:
    """模拟行内 createSceneApiKey.do（fetch_ellm_key 的请求/响应协议）。

    按场景码签发 api_key —— 直接签发 DeepSeek key，使"携平台签发的 key
    调用平台"（即本网关的 chat 代理）走真实模型。
    """
    req = json.loads(REQ_MESSAGE)
    scene_code = req.get("REQ_BODY", {}).get("param", {}).get("sceneCode", "")
    print(f"[mock-gateway] scene_code={scene_code!r} -> 签发 DeepSeek key")
    return JSONResponse(
        {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {
                "result": {
                    "apiKey": _deepseek_key(),
                    "timeToLive": _ISSUED_TTL_MS,
                },
            },
        },
    )


if __name__ == "__main__":
    _load_dotenv()
    uvicorn.run("dev_ellm_gateway:app", host="0.0.0.0", port=8001)
