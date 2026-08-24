# -*- coding: utf-8 -*-
"""ActiveSkillMiddleware —— 解析用户消息中的 ``/skill_name`` 前缀。

用户输入 ``/skill_name 问题`` 时：
1. 从请求体 ``input.content`` 的 text block 中解析出 ``skill_name`` 与剩余问题；
2. 把 ``skill_name`` 存入 ContextVar（供 CustomPromptMiddleware 读取）；
3. 把去掉 ``/skill_name`` 前缀后的消息写回请求体，LLM 只看到问题。

技能不存在时：skill_name 仍会被设置，由 CustomPromptMiddleware 判断技能
存在性——不存在则按普通消息处理（不注入）。
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Any

_active_skill: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_skill",
    default="",
)


def get_active_skill() -> str:
    """返回当前请求指定的技能名（无则空串）。"""
    return _active_skill.get() or ""


_SKILL_PREFIX_RE = re.compile(r"^/(\S+)\s*(.*)$", re.S)


def _rewrite_question(m: re.Match[str]) -> str:
    """把 ``/skill_name 问题`` 重写为显式要求使用该技能的任务描述。"""
    skill_name = m.group(1)
    question = m.group(2).strip() or "请使用该技能完成工作"
    return f"请使用 {skill_name} 技能完成以下任务：{question}"


def _match_text_blocks(blocks: Any) -> tuple[str, bool]:
    """在 block 数组（list[dict]）的 text block 里匹配 ``/skill_name`` 前缀。

    命中时原地改写 ``block["text"]`` 为显式任务描述。

    Returns:
        ``(skill_name, modified)``：技能名与是否发生了改写。
    """
    if not isinstance(blocks, list):
        return "", False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        m = _SKILL_PREFIX_RE.match(text)
        if m:
            block["text"] = _rewrite_question(m)
            return m.group(1), True
    return "", False


class ActiveSkillMiddleware:
    """ASGI 中间件：解析 ``/skill_name`` 前缀，移除并存入 ContextVar。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 仅处理 chat 请求（fire-and-forget 入口 / deerflow run stream 入口）
        path = scope.get("path", "")
        if not (
            path.endswith("/chat/")
            or path.endswith("/chat")
            or path.endswith("/runs/stream")
        ):
            await self.app(scope, receive, send)
            return

        # 1. 读取 body
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break

        raw = b"".join(chunks)
        new_raw, skill_name = self._parse_skill_prefix(raw)
        if skill_name:
            _active_skill.set(skill_name)

        # 2. 重放（修改后的）body 给下游
        async def new_receive():
            yield {"type": "http.request", "body": new_raw, "more_body": False}

        recv_iter = new_receive()

        async def receive_wrapper() -> dict:
            try:
                return await recv_iter.__anext__()
            except StopAsyncIteration:
                # 重放完 body 后，继续转发原始 receive 的后续消息，而不是
                # 返回 http.disconnect。否则对 /runs/stream 这类 SSE 流式
                # 请求，Starlette 会把该消息误判为客户端断开，提前终止
                # StreamingResponse（表现为只回显 human 帧后 SSE 直接结束）。
                return await receive()

        await self.app(scope, receive_wrapper, send)

    @staticmethod
    def _parse_skill_prefix(raw: bytes) -> tuple[bytes, str]:
        """解析 ``input.content`` 中 text block 的 ``/skill_name`` 前缀。

        Returns:
            ``(new_raw, skill_name)``：去掉前缀后的请求体，与技能名。
            请求携带 ``custom_prompt`` 时：不重写消息、不设置技能名
            （由 custom_prompt 整体覆盖 system prompt）。
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return raw, ""

        # 带 custom_prompt → 不重写用户消息（custom_prompt 优先）
        custom_prompt = data.get("custom_params", {}).get("custom_prompt")
        if custom_prompt:
            return raw, ""

        input_data = data.get("input")
        if not isinstance(input_data, dict):
            return raw, ""

        skill_name = ""
        modified = False

        # 原生 /chat/：input.content 为 block 数组
        content = input_data.get("content")
        if isinstance(content, list):
            skill_name, modified = _match_text_blocks(content)

        # deerflow /runs/stream：input.messages 为 LangGraph 消息数组，
        # 每条 content 可能是字符串或 block 数组
        if not modified:
            messages = input_data.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    msg_content = message.get("content")
                    if isinstance(msg_content, str):
                        m = _SKILL_PREFIX_RE.match(msg_content)
                        if m:
                            message["content"] = _rewrite_question(m)
                            skill_name = m.group(1)
                            modified = True
                            break
                    elif isinstance(msg_content, list):
                        skill_name, modified = _match_text_blocks(msg_content)
                        if modified:
                            break

        if modified:
            return (
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                skill_name,
            )
        return raw, ""
