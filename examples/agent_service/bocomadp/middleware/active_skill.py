# -*- coding: utf-8 -*-
"""ActiveSkillMiddleware —— 解析用户消息中的 ``@skill:<name>`` 标记。

标记协议（可同时指定多个技能）::

    @skill:ob-sql-review 帮我看下这段 SQL
    @skill:ob-sql-review @skill:excel分析 对比下两份数据

- 标记形如 ``@skill:<技能名>``，``@skill:`` 关键字**大小写不敏感**；
- 技能名到**空白**或 ``@`` 为止，且必须由**空白（或消息结尾）收尾**——
  因此 ``@skill:a@skill:b`` 这种没有空白分隔的写法**整段不识别**；
- 标记可出现在消息**任意位置**（正文中间也识别，前面不要求有空格）；
- 识别到的技能名**去重**后按出现顺序汇总。

命中标记时：
1. 把技能名列表存入 ContextVar（:func:`get_active_skills`，供下游读取）；
2. **消息正文一个字符都不改**——``@skill:<name>`` 标记随用户原文一起
   落库（前端历史/刷新后看到的就是用户输入的原始字符串）；
3. 模型侧如何得知"本次指定了哪些技能"由
   :class:`CustomPromptMiddleware` 在 system prompt 中注入
   （见 ``custom_prompt.py::_build_active_skill_note``）。

边界与约定：
- 本中间件对请求体**只读不写**：解析完标记后把原始 body 原样重放给下游；
- system prompt 的技能注入保持框架默认的 **全量** ``<agent-skills>``
  段，另加一段"本次请求指定技能"的强调；
- 不校验技能是否存在；
- 请求携带 ``custom_params.custom_prompt`` 时**不解析**（custom_prompt
  整体覆盖 system prompt，优先于本中间件）；
- **仅拦截 chat 入口**：``/chat/``、``/chat``
  （deerflow 的 ``/runs/stream`` 不再拦截）。

历史演进：旧协议为消息开头的 ``/skill_name`` 前缀；随后一度把消息改写为
"请使用 xxx 技能完成以下任务：…"（该文案会落库并被前端展示）；再改为
"摘掉标记、只保留问题"；**现为"标记原样保留、正文不动"**。
"""
from __future__ import annotations

import contextvars
import json
from typing import Any

#: 当前请求指定的技能名（按出现顺序去重）；无则空元组。
_active_skills: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "active_skills",
    default=(),
)

#: 标记前缀（匹配时大小写不敏感）。
_MARKER_PREFIX = "@skill:"


def get_active_skills() -> list[str]:
    """返回当前请求指定的全部技能名（无则空列表）。"""
    return list(_active_skills.get() or ())


def get_active_skill() -> str:
    """返回第一个指定技能名（兼容旧接口；无则空串）。"""
    skills = _active_skills.get() or ()
    return skills[0] if skills else ""


def _dedup(names: list[str]) -> list[str]:
    """按出现顺序去重（保留首现位置）。"""
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _scan_skills(text: str) -> list[str]:
    """扫描文本中所有合法的 ``@skill:<name>`` 标记，返回技能名。

    纯读取，不改动传入文本。技能名按出现顺序返回、**未去重**。

    识别规则：
        - ``@skill:`` 大小写不敏感；
        - 技能名到空白或 ``@`` 为止，且必须由**空白（或文本结尾）收尾**；
        - 非法标记（如 ``@skill:a@skill:b`` 无空白分隔）整段跳过，不把其中
          的第二个 ``@skill:`` 误判为有效技能。
    """
    if _MARKER_PREFIX not in text.lower():
        return []

    lowered = text.lower()
    names: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        hit = lowered.find(_MARKER_PREFIX, index)
        if hit < 0:
            break

        start = hit + len(_MARKER_PREFIX)
        cursor = start
        while cursor < length and not text[cursor].isspace() and text[cursor] != "@":
            cursor += 1

        name = text[start:cursor]
        if name and (cursor >= length or text[cursor].isspace()):
            names.append(name)
            index = cursor
        else:
            # 非法标记：跳过这一段连续非空白，避免误判其中的后续标记
            cursor = start
            while cursor < length and not text[cursor].isspace():
                cursor += 1
            index = cursor

    return names


def _collect_skill_names(data: dict) -> list[str]:
    """从请求体中收集技能名（只读，不修改任何内容）。

    支持原生 ``/chat/`` 的 ``input.content`` block 数组（遍历其中的
    ``type == "text"`` 块）。
    """
    input_data = data.get("input")
    if not isinstance(input_data, dict):
        return []

    content = input_data.get("content")
    if not isinstance(content, list):
        return []

    names: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            names.extend(_scan_skills(text))
    return _dedup(names)


class ActiveSkillMiddleware:
    """ASGI 中间件：解析 ``@skill:<name>`` 标记并存入 ContextVar（只读）。

    请求体不做任何修改，解析后原样重放给下游。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 仅处理 chat 入口（原生 fire-and-forget 与阻塞式 chat）
        path = scope.get("path", "")
        if not (path.endswith("/chat/") or path.endswith("/chat")):
            await self.app(scope, receive, send)
            return

        # 1. 读取 body（只为了解析标记，内容不会被修改）
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break

        raw = b"".join(chunks)
        skill_names = self._parse_skill_names(raw)
        if skill_names:
            _active_skills.set(tuple(skill_names))

        # 2. 原样重放 body 给下游
        async def new_receive():
            yield {"type": "http.request", "body": raw, "more_body": False}

        recv_iter = new_receive()

        async def receive_wrapper() -> dict:
            try:
                return await recv_iter.__anext__()
            except StopAsyncIteration:
                # 重放完 body 后，继续转发原始 receive 的后续消息，而不是
                # 返回 http.disconnect。否则对 SSE 流式响应，Starlette 会把
                # 该消息误判为客户端断开，提前终止 StreamingResponse。
                return await receive()

        await self.app(scope, receive_wrapper, send)

    @staticmethod
    def _parse_skill_names(raw: bytes) -> list[str]:
        """从请求体中解析 ``@skill:`` 标记，返回去重后的技能名列表。

        **不修改** 请求体：仅解析 ``input.content`` 里的 text block。

        Returns:
            ``list[str]``: 技能名（按出现顺序去重）；body 非法、无 ``input``、
            携带 ``custom_prompt`` 或未命中标记时返回空列表。
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 非 JSON / 编码异常：视为无标记
            return []

        if not isinstance(data, dict):
            return []

        # 带 custom_prompt → 不解析（custom_prompt 优先，整体覆盖 system prompt）
        custom_prompt = (data.get("custom_params") or {}).get("custom_prompt")
        if custom_prompt:
            return []

        return _collect_skill_names(data)


__all__ = [
    "ActiveSkillMiddleware",
    "get_active_skill",
    "get_active_skills",
]
