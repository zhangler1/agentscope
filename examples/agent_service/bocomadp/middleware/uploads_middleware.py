# -*- coding: utf-8 -*-
"""UploadsMiddleware —— 将上传文件以「大纲 + 虚拟路径引用」注入 human 消息。

对应 deer-flow 的 UploadsMiddleware（基于 HumanInputMiddleware）。
本框架使用 AgentScope 的 ``MiddlewareBase.on_model_call`` 洋葱钩子，
在模型调用前改写真实存在的 ``input_kwargs["messages"]``，把该轮上传文件
渲染成 ``<context name="files">`` 文本块追加到对应 human 消息里。

⚠️ 早期实现挂在 ``on_reply`` 上——但该钩子的 input_kwargs 仅有 ``inputs`` /
``structured_schema``（消息在 _reply_impl 内组装），``input_kwargs.get("messages")``
永远为空，注入从未生效（详见 docs/custom_params.md「历史大坑」）。``on_model_call``
才是消息列表真实存在且可改写的落点（tool_result_budget / tool_call_repair 等同伴
中间件均在此改写 messages）。

注入策略（对照 Plan 第 4 节，已修正为 outline + 引用，而非内联全文）：
- 从 human 消息 ``metadata["files"]`` 取出文件列表；
- 优先用转换后的同名 ``.md`` 生成 outline（file_outline.create_outline）；
- 用 ``<context name="files">`` 包裹大纲 + 虚拟路径引用；
- 无 ``.md`` 时仅注入文件名 + 虚拟路径引用（Agent 用工具读原始文件）。
"""
from __future__ import annotations

import logging
from typing import Any

try:
    from bocomadp.middleware.agent_middleware import MiddlewareBase
except Exception:  # pragma: no cover - agentscope 不可用时降级（如纯单测环境）
    class MiddlewareBase:  # type: ignore
        """最小兜底基类：仅在 AgentScope 不可用时使用，保证可导入与单测。"""

        async def on_model_call(self, agent, input_kwargs, next_handler):
            return await next_handler(**input_kwargs)

from bocomadp.uploads.db import get_uploads_db
try:
    from agentscope.message import TextBlock
except Exception:  # pragma: no cover - agentscope 不可用时降级（如纯单测环境）
    TextBlock = None  # type: ignore

from bocomadp.uploads.manager import to_virtual_path
from bocomadp.uploads.file_outline import create_outline, create_outline_text

logger = logging.getLogger(__name__)


class UploadsMiddleware(MiddlewareBase):
    """人类输入中间件：把上传文件作为上下文注入。"""

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Any,
    ) -> Any:
        """模型调用前注入本轮回传文件上下文。

        ``on_model_call`` 的 input_kwargs 含真实 ``messages`` 列表（与
        ``on_reply`` 不同，后者只有 ``inputs``）。一次回复内每调用一次模型就会
        触发一次本钩子（思考→调工具→再思考循环），因此注入必须幂等：每条
        human 消息仅注入一次，用 ``metadata["_uploads_injected"]`` 标记，避免
        同一消息在多次模型调用间被重复追加 ``<context name="files">``。
        """
        messages = input_kwargs.get("messages")
        if not messages:
            return await next_handler(**input_kwargs)

        # 只处理「最后一条带文件且尚未注入」的 human 消息（与原始语义一致：
        # 仅注入最近一轮的上传文件，不重复历史轮次）。
        target = self._pending_target(messages)
        if target is None:
            return await next_handler(**input_kwargs)

        files = (self._meta(target) or {}).get("files") or []
        if not files:
            return await next_handler(**input_kwargs)

        # 优先从 agent.state 取当前会话上下文（方案 A 下虚拟路径不再编码
        # user/session，定位记录需依赖会话上下文）。
        ctx_session = getattr(getattr(agent, "state", None), "session_id", "") or ""
        ctx_user = getattr(getattr(agent, "state", None), "user_id", "") or ""
        ctx_agent = getattr(getattr(agent, "state", None), "agent_id", "") or ""

        blocks = []
        for fmeta in files:
            block = self._render_file_block(
                fmeta,
                user_id=ctx_user,
                session_id=ctx_session,
                agent_id=ctx_agent,
            )
            if block:
                blocks.append(block)

        if blocks:
            usage_hint = (
                "\n\n提示：要列出本会话全部已上传文件，可调用 "
                "list_uploaded_files()（框架会自动注入当前 user_id / session_id）；"
                "需读取全文时调用 read_uploaded_file(virtual_path=...)，并同样传入"
                "当前会话的 user_id / session_id；图片文件请调用 "
                "view_image_tool(virtual_path=..., question=用户的问题)。"
            )
            injection = (
                "<context name=\"files\">\n"
                + "\n\n".join(blocks)
                + usage_hint
                + "\n</context>"
            )
            self._append_block(target, injection)
            self._meta(target)["_uploads_injected"] = True
            logger.info(
                "UploadsMiddleware injected %d file block(s) into model context",
                len(blocks),
            )

        return await next_handler(**input_kwargs)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _role(msg: Any) -> Any:
        if isinstance(msg, dict):
            return msg.get("role") or msg.get("name")
        return getattr(msg, "role", None) or getattr(msg, "name", None)

    @staticmethod
    def _meta(msg: Any) -> dict:
        """返回消息的 metadata dict（兼容 Msg 对象与 dict 消息）。"""
        if isinstance(msg, dict):
            m = msg.get("metadata")
            if not isinstance(m, dict):
                m = msg.setdefault("metadata", {})
            return m
        m = getattr(msg, "metadata", None)
        if not isinstance(m, dict):
            try:
                msg.metadata = {}
                m = msg.metadata
            except Exception:  # noqa: BLE001
                return {}
        return m

    @staticmethod
    def _pending_target(messages: list) -> Any:
        """倒序找最后一条带 files 且尚未注入的 human 消息。

        返回该消息对象；若没有（无文件 / 最近一条带文件消息已注入）则返回
        None。仅处理「最后一条带文件 human」，与原始 _extract_files 的语义
        一致（只注入最近一轮的上传文件，不重复历史轮次）。
        """
        for msg in reversed(messages):
            if UploadsMiddleware._role(msg) in ("user", "human"):
                meta = UploadsMiddleware._meta(msg)
                if not isinstance(meta, dict):
                    continue
                if meta.get("_uploads_injected"):
                    # 最近一条带文件 human 已经注入过（同一回复内的后续模型调用）
                    return None
                if meta.get("files"):
                    return msg
        return None

    @staticmethod
    def _render_file_block(
        fmeta: dict,
        user_id: str = "",
        session_id: str = "",
        agent_id: str = "",
    ) -> str:
        filename = fmeta.get("filename") or (fmeta.get("virtual_path") or "").rsplit("/", 1)[-1]
        virtual_path = fmeta.get("virtual_path") or ""
        if not virtual_path:
            return ""

        # 沙箱模式下 .md 位于沙箱内，中间件（同步 ASGI 层）无法直接读取，
        # 因此上传时在 UploadedFile.markdown 列已固化大纲文本，此处直接取用。
        # 方案 A 下虚拟路径不再编码 session，优先用 (user_id, session_id,
        # stored_name) 定位；前端 fmeta 可能自带这些字段（含 stored_name）。
        stored_name = fmeta.get("stored_name") or ""
        if not stored_name:
            try:
                from bocomadp.uploads.manager import resolve_upload_parts
                _, _, stored_name = resolve_upload_parts(virtual_path)
            except Exception:  # noqa: BLE001
                stored_name = filename

        u = user_id or fmeta.get("user_id", "")
        s = session_id or fmeta.get("session_id", "")
        a = agent_id or fmeta.get("agent_id", "")
        record = None
        if u and s and stored_name:
            try:
                record = get_uploads_db().get_by_session_file(u, s, stored_name, a)
            except Exception:  # noqa: BLE001
                record = None
        if record is None:
            try:
                record = get_uploads_db().get_by_virtual_path(virtual_path)
            except Exception as e:  # 元数据缺失：仅给文件名 + 路径引用
                logger.warning("skip file (metadata miss): %s (%s)", virtual_path, e)
                return (
                    f"- 文件: {filename}\n"
                    f"  虚拟路径: {virtual_path}\n"
                    f"  (暂无可预览文本，请使用工具读取原始文件)"
                )

        if record and record.is_image:
            # 图片：上传时已固化为 base64（view_image_tool 从元数据直读），
            # 正文不可内联预览，提示 Agent 调用图片解析工具。
            return (
                f"- 文件: {filename} [图片]\n"
                f"  虚拟路径: {virtual_path}\n"
                f"  (图片内容不可内联预览；如需解析图片，请调用 "
                f"view_image_tool 并传入上述 virtual_path 与用户的问题)"
            )

        if record and record.markdown:
            outline = create_outline_text(record.markdown).strip()
            if outline:
                return (
                    f"- 文件: {filename}\n"
                    f"  虚拟路径: {virtual_path}\n"
                    f"  大纲/预览:\n{outline}\n"
                    f"  (如需全文，请使用 list_uploaded_files / read 工具按虚拟路径读取)"
                )
        # 无 .md 时仅给文件名 + 路径引用
        return (
            f"- 文件: {filename}\n"
            f"  虚拟路径: {virtual_path}\n"
            f"  (暂无可预览文本，请使用工具读取原始文件)"
        )

    @staticmethod
    def _append_block(msg: Any, text: str) -> None:
        """把一个文本块追加到指定消息的 content 末尾（兼容 Msg 对象与 dict）。"""
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = f"{content}\n\n{text}"
            elif isinstance(content, list):
                content.append({"type": "text", "text": text})
            return
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            msg.content = f"{content}\n\n{text}"
        elif isinstance(content, list):
            # Msg 对象：content 为 ContentBlock 对象列表（新版），
            # 也可能混入 dict（旧版序列化形态），统一追加文本块。
            block = (
                TextBlock(text=text)
                if TextBlock is not None
                else {"type": "text", "text": text}
            )
            content.append(block)


# 模块级实例：MiddlewareRegistry.load_builtin() 会自动扫描并注册，
# 与 LoggingMiddleware 等并列，无需改 factory.py。
uploads_mw = UploadsMiddleware()
