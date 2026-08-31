# -*- coding: utf-8 -*-
"""本地开发用 Mock ELLM 网关（OpenAI 兼容 chat completions + 取 key 服务）。

行内凭证（``base_url`` / ``api_key`` / ``api_key_url`` 指向行内）只在行内
网络生效。在本地验证 bocom-starter 的行内模型流程时，用本脚本在宿主机起
一个假的 ELLM 网关，把凭证的 ``base_url`` / ``api_key_url`` 指向它即可
走通全链路（含 api_key 刷新中间件）。

启动::

    python dev_ellm_gateway.py        # uvicorn 0.0.0.0:8001

然后创建测试凭证（``POST /credential``）：

- 本地直跑 bocom-starter：``base_url=http://localhost:8001/v1``
- Docker 容器内访问宿主机：``base_url=http://host.docker.internal:8001/v1``
  （Linux 主机需在 compose agentscope 服务加
  ``extra_hosts: ["host.docker.internal:host-gateway"]``）

提供两个端点：

- ``POST /v1/chat/completions`` —— OpenAI 兼容（stream / non-stream），
  回复内容回显用户消息，便于确认请求到达；
- ``POST /v1/keys`` —— 模拟行内 ``createSceneApiKey.do``（表单
  ``REQ_MESSAGE`` + ``TRAN_SUCCESS`` 响应），用于验证 api_key 刷新链路。
"""
from __future__ import annotations

import json
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock ELLM Gateway")

_MOCK_KEY = "mock-ellm-key-123"
# 模拟行内签发的 key 有效期（毫秒），刷新链路写回 apikey_expires_at 用。
_MOCK_TTL_MS = 1_500_000


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    """OpenAI 兼容 chat completions：流式与非流式均支持。"""
    body = await request.json()
    messages = body.get("messages", [])
    last_user = next(
        (
            m.get("content", "")
            for m in reversed(messages)
            if m.get("role") == "user"
        ),
        "",
    )
    reply = f"（mock）已收到：{last_user}"
    stream = body.get("stream", False)

    if not stream:
        return JSONResponse(
            {
                "id": "mock-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "content": reply,
                            "reasoning_content": None,
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    async def _events() -> Any:
        def _chunk(
            delta_text: str | None = None,
            has_choices: bool = True,
            usage: dict | None = None,
        ) -> str:
            chunk: dict = {
                "id": "mock-1",
                "choices": (
                    [
                        {
                            "index": 0,
                            "delta": {
                                "content": delta_text,
                                "reasoning_content": None,
                                "tool_calls": None,
                            },
                            "finish_reason": None,
                        }
                    ]
                    if has_choices
                    else []
                ),
                "usage": usage,
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        yield _chunk(delta_text=reply)
        yield _chunk(
            has_choices=False,
            usage={"prompt_tokens": 10, "completion_tokens": 3},
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/keys")
async def create_scene_api_key(REQ_MESSAGE: str = Form(...)) -> Any:
    """模拟行内 createSceneApiKey.do（fetch_ellm_key 的请求/响应协议）。"""
    req = json.loads(REQ_MESSAGE)
    scene_code = req.get("REQ_BODY", {}).get("param", {}).get("sceneCode", "")
    return JSONResponse(
        {
            "RSP_HEAD": {"TRAN_SUCCESS": "1"},
            "RSP_BODY": {
                "result": {
                    "apiKey": f"{_MOCK_KEY}-{scene_code}-{int(time.time())}",
                    "timeToLive": _MOCK_TTL_MS,
                }
            },
        },
    )


if __name__ == "__main__":
    uvicorn.run("dev_ellm_gateway:app", host="0.0.0.0", port=8001)
