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


class ActiveSkillMiddleware:
    """ASGI 中间件：解析 ``/skill_name`` 前缀，移除并存入 ContextVar。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 仅处理 chat 请求（fire-and-forget 入口）
        path = scope.get("path", "")
        if not (path.endswith("/chat/") or path.endswith("/chat")):
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
                return {"type": "http.disconnect"}

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
        content = input_data.get("content")
        if not isinstance(content, list):
            return raw, ""

        modified = False
        skill_name = ""
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            m = _SKILL_PREFIX_RE.match(text)
            if m:
                skill_name = m.group(1)
                question = m.group(2).strip() or "请使用该技能完成工作"
                # 重写用户消息：显式要求使用该技能，提升 LLM 调用率
                block["text"] = (
                    f"请使用 {skill_name} 技能完成以下任务：{question}"
                )
                modified = True
                break

        if modified:
            return (
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                skill_name,
            )
        return raw, ""
