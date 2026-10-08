# -*- coding: utf-8 -*-
"""ChatUploadInjectMiddleware —— 校验并落库前端显式携带的上传文件引用。

前端时序：先 ``POST /files/upload`` 批量上传（返回 ``UploadedFile`` 数组），
再 ``POST /chat/`` 时把该数组原样放进请求体 ``input.metadata.files``（最少
只需每项的 ``virtual_path``）。本 ASGI 中间件逐个引用到 ``uploaded_files``
元数据表核对归属，通过后规范化为 ``{filename, filetype, virtual_path}``
三键写回 ``input.metadata.files``：

- ``UploadsMiddleware``（on_model_call）按原设计读 ``metadata["files"]``
  注入 ``<context name="files">``，本中间件对它透明；
- human 消息落库后，``GET /api/sessions/{id}/messages`` 即可读到每轮各自的
  文件列表，前端逐轮还原「哪一轮除了文字还上传了哪些文件」。

安全边界
--------
文件引用来自客户端、不可信任：每个 ``virtual_path`` 必须能在 uploads 元数据
表查到，且记录的 ``user_id`` / ``session_id`` 与当前请求一致，否则丢弃该
引用并记日志——防止 A 用户借 B 用户的 ``virtual_path`` 越权读文件。

前端未传 ``files``（或全部无效）时不做任何注入，也不回退查库猜测——按轮
归因由前端显式声明，后端只做校验与规范化。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from ..uploads.db import get_uploads_db

# 复用 ``as`` logger 通道：main.py 已为 ``as`` 挂载 events.log 滚动 handler
# 并自动注入 trace_id，与 MODEL_*/TOOL_* 事件同文件。若用默认 ``__name__``
# （bocomadp.*），日志只到 stdout、不进 events.log，日后排查易被误判为「0 次」。
logger = logging.getLogger("as")

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ChatUploadInjectMiddleware:
    """拦截 ``POST /chat/``，校验并规范化前端携带的 ``input.metadata.files``。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # 框架 chat 路由真实路径为 ``/api/chat/``（FastAPI 全局前缀
        # ``/api`` + chat_router 的 ``/chat``）；``/chat`` 无 ``/api`` 前缀
        # 时为 404。这里两种都放行，避免 path 不匹配导致中间件静默跳过。
        if path.rstrip("/") not in ("/api/chat", "/chat"):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.debug("chat body is not valid JSON, skip upload injection")
            await self.app(scope, _wrap_body(body), send)
            return

        user_id = _get_header(scope, b"x-user-id")
        if not user_id:
            logger.info("[upload-inject] skip: x-user-id header missing")
            await self.app(scope, _wrap_body(body), send)
            return

        logger.info(
            "[upload-inject] processing chat path=%s session=%s agent=%s user=%s",
            path,
            payload.get("session_id"),
            payload.get("agent_id"),
            user_id,
        )

        if await _apply_client_files(payload, user_id):
            new_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            _set_content_length(scope, len(new_body))
            logger.info(
                "normalized %d uploaded file ref(s) in chat request "
                "session=%s agent=%s user=%s",
                len(payload["input"]["metadata"]["files"]),
                payload.get("session_id"),
                payload.get("agent_id"),
                user_id,
            )
        else:
            new_body = body

        await self.app(scope, _wrap_body(new_body), send)


# ---------------------------------------------------------------------------
# 请求体读写助手
# ---------------------------------------------------------------------------
async def _read_body(receive: Receive) -> bytes:
    """把 ASGI ``receive`` 里的 HTTP body 完整读出来。"""
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        msg_type = msg.get("type")
        if msg_type == "http.request":
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        elif msg_type == "http.disconnect":
            break
    return b"".join(chunks)


def _wrap_body(body: bytes) -> Receive:
    """构造一个已经把 ``body`` 准备好的 ``receive`` 可调用对象。"""
    messages: list[Message] = [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]
    index = 0

    async def receive() -> Message:
        nonlocal index
        if index < len(messages):
            msg = messages[index]
            index += 1
            return msg
        return {"type": "http.disconnect"}

    return receive


def _get_header(scope: Scope, name: bytes) -> str:
    """从 ASGI scope headers 中读指定 header（大小写不敏感）。"""
    for key, value in scope.get("headers", []):
        if key.lower() == name.lower():
            return value.decode("utf-8", errors="replace")
    return ""


def _set_content_length(scope: Scope, length: int) -> None:
    """更新/追加 ``Content-Length``，避免 downstream 读到旧长度。"""
    headers = list(scope.get("headers", []))
    new_headers: list[tuple[bytes, bytes]] = []
    found = False
    for key, value in headers:
        if key.lower() == b"content-length":
            new_headers.append((key, str(length).encode("utf-8")))
            found = True
        else:
            new_headers.append((key, value))
    if not found:
        new_headers.append((b"content-length", str(length).encode("utf-8")))
    scope["headers"] = new_headers


# ---------------------------------------------------------------------------
# 文件引用校验与规范化
# ---------------------------------------------------------------------------
async def _apply_client_files(payload: dict[str, Any], user_id: str) -> bool:
    """校验前端携带的 ``input.metadata.files``，规范化后写回。

    前端 refs 接受 ``POST /files/upload`` 响应对象原样传入（多余字段忽略），
    最少只需 ``virtual_path``。每个引用都到 ``uploaded_files`` 表核对归属
    （记录的 user_id / session_id 必须与当前请求一致），非法引用丢弃并记
    warning；全部无效时不改动 payload。

    Returns:
        是否实际改动了 payload。
    """
    session_id = payload.get("session_id")
    input_msg = payload.get("input")
    if not session_id:
        logger.debug("[upload-inject] skip: missing session_id")
        return False

    target = _find_last_human_message(input_msg)
    if target is None:
        logger.debug("[upload-inject] skip: no human message in input")
        return False

    meta = target.get("metadata")
    refs = meta.get("files") if isinstance(meta, dict) else None
    if not isinstance(refs, list) or not refs:
        logger.debug(
            "[upload-inject] skip: input carries no files "
            "(explicit refs required)"
        )
        return False

    files, dropped = await asyncio.to_thread(
        _validate_refs_sync, refs, user_id, str(session_id)
    )
    if dropped:
        logger.warning(
            "[upload-inject] dropped %d invalid file ref(s) "
            "session=%s user=%s dropped=%s",
            len(dropped),
            session_id,
            user_id,
            dropped,
        )
    if not files:
        logger.info(
            "[upload-inject] skip: 0 valid file refs session=%s user=%s",
            session_id,
            user_id,
        )
        return False

    if not isinstance(target.get("metadata"), dict):
        target["metadata"] = {}
    target["metadata"]["files"] = files
    return True


def _validate_refs_sync(
    refs: list[Any],
    user_id: str,
    session_id: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """逐个引用查 uploads 元数据表核对归属，规范化为三键。

    同步 SQLite 查询，调用方置于线程（``asyncio.to_thread``）以免阻塞事件循环。

    Returns:
        (合法引用的规范化列表, 被丢弃引用的标识列表)。
    """
    files: list[dict[str, str]] = []
    dropped: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            dropped.append(str(ref)[:80])
            continue
        virtual_path = str(ref.get("virtual_path") or "").strip()
        if not virtual_path:
            dropped.append("<missing virtual_path>")
            continue
        try:
            # 三键（user/session/virtual_path）查询：命中即代表该引用属于
            # 当前用户当前会话，同时避免同名 virtual_path 多记录时误命中。
            record = get_uploads_db().get_by_session_virtual_path(
                user_id, session_id, virtual_path
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to query uploads db for virtual_path=%s", virtual_path
            )
            record = None
        if record is None:
            dropped.append(virtual_path)
            continue
        files.append(
            {
                "filename": record.original_name,
                "filetype": record.content_type or "application/octet-stream",
                "virtual_path": record.virtual_path,
            }
        )
    return files, dropped


def _find_last_human_message(input_msg: Any) -> dict[str, Any] | None:
    """从 ``input`` 里找到最后一条 human/user 消息。"""
    if isinstance(input_msg, dict) and input_msg.get("role") == "user":
        return input_msg
    if isinstance(input_msg, list):
        for msg in reversed(input_msg):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return msg
    return None
