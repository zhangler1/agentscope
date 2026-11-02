# -*- coding: utf-8 -*-
"""UploadsMiddleware —— 将上传文件以「大纲 + 虚拟路径引用」注入 human 消息。

对应 deer-flow 的 UploadsMiddleware（基于 HumanInputMiddleware）。
本框架使用 AgentScope 的 ``MiddlewareBase.on_model_call`` 钩子：框架三个
洋葱钩子中只有 ``on_model_call`` 的 ``input_kwargs`` 携带 ``messages``
（on_reply 传 ``inputs``、on_reasoning 传 ``tool_choice``），注入因此挂在
on_model_call——真实模型调用前的最后一环改写 ``input_kwargs["messages"]``，
且注入只作用于本次调用快照、不污染 ``agent.state.context``。

注入策略（对照 Plan 第 4 节，已修正为 outline + 引用，而非内联全文）：
- 从最后一条 human 消息的 ``metadata.files``（或旧版 additional_kwargs）
  取出文件列表；
- 优先用 uploads DB 固化的 markdown 生成 outline
  （file_outline.create_outline_text）；
- 用 ``<context name="files">`` 包裹大纲 + 虚拟路径引用；
- 无 ``.md`` 时仅注入文件名 + 虚拟路径引用（Agent 用工具读原始文件）。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Awaitable, Callable

try:
    from bocomadp.middleware.agent_middleware import MiddlewareBase
except Exception:  # pragma: no cover - agentscope 不可用时降级（如纯单测环境）
    class MiddlewareBase:  # type: ignore
        """最小兜底基类：仅在 AgentScope 不可用时使用，保证可导入与单测。"""

        async def on_reply(self, agent, input_kwargs, next_handler):
            async for event in next_handler():
                yield event

        async def on_model_call(self, agent, input_kwargs, next_handler):
            return await next_handler()

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
        next_handler: Callable[..., Awaitable[Any]],
    ) -> Any:
        """``on_model_call`` 钩子：注入文件上下文 + 透传模型调用。

        注意：本钩子是 **async 函数**（**不能 yield**），框架用
        ``await mw.on_model_call(...)`` 接收返回值——对齐
        EventLogMiddleware 的实现形态；流式模型返回 AsyncGenerator，
        需内嵌 wrapper 透传。
        """
        messages = input_kwargs.get("messages")
        if messages:
            self._inject_files_context(agent, messages)

        result = await next_handler()
        if hasattr(result, "__aiter__"):

            async def _wrapped() -> AsyncGenerator[Any, None]:
                async for chunk in result:
                    yield chunk

            return _wrapped()
        return result

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _inject_files_context(self, agent: Any, messages: list) -> None:
        """从最后一条 human 消息提取 files 元数据并注入 ``<context name="files">``。

        注入在消息副本上进行（messages 是 ``_prepare_model_input`` 组装的
        本次调用快照），替换 messages 列表中的引用；随后消费原消息的 files
        元数据保证幂等（同一 run 内多轮工具循环的后续 model call 不会重复
        注入同一批文件）。
        """
        found = self._find_last_human(messages)
        if found is None:
            return
        _, human_msg = found
        files = self._files_from_msg(human_msg)
        if not files:
            return

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

        if not blocks:
            return
        usage_hint = (
            "\n\n提示：上传文件位于工作目录下的 user-data/uploads/ 目录。"
            "沙箱内请用相对该目录的路径读取：Bash 用 ls user-data/uploads/ "
            "列出文件；Glob 用 path=user-data/uploads；Read 需绝对路径，"
            "请用你的工作目录拼接（user-data/uploads/xxx 相对工作目录）。"
            "图片文件请调用 "
            "view_image_tool(virtual_path=..., question=用户的问题)。"
        )
        injection = (
            "<context name=\"files\">\n"
            + "\n\n".join(blocks)
            + usage_hint
            + "\n</context>"
        )
        # 幂等：先消费原消息的 files 元数据，同 run 后续 model call 不再
        # 注入（须在副本化之前：Msg 深拷贝会连带复制 metadata，副本若仍
        # 携带 files，下一轮 model call 会重复注入）。
        self._consume_files(human_msg)
        # 副本注入（替换 messages 引用，不污染 state.context）
        self._append_to_last_human(messages, injection)
        logger.info("UploadsMiddleware injected %d file block(s)", len(blocks))

    @staticmethod
    def _find_last_human(messages: list) -> tuple[int, Any] | None:
        """倒序找最后一条 user/human 消息，返回 ``(索引, 消息)``。"""
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            if isinstance(msg, dict):
                role = msg.get("role") or msg.get("name")
            else:
                role = getattr(msg, "role", None) or getattr(msg, "name", None)
            if role in ("user", "human"):
                return idx, msg
        return None

    @staticmethod
    def _files_from_msg(msg: Any) -> list[dict]:
        """兼容对象消息与 dict 消息两种形态；新版 Msg 用 metadata 取代
        旧版 additional_kwargs 承载自定义字段。"""
        f = getattr(msg, "additional_kwargs", None)
        if f is None and isinstance(msg, dict):
            f = msg.get("additional_kwargs")
        if f is None:
            f = getattr(msg, "metadata", None)
        if isinstance(f, dict) and f.get("files"):
            return f["files"]
        return []

    @staticmethod
    def _consume_files(msg: Any) -> None:
        """清空消息的 files 元数据（只删 files 键，保留其余字段）。

        ``on_model_call`` 每轮模型调用都触发（工具循环内多轮）；注入后
        消费 files 元数据，后续 model call 提取不到 files 即不会重复注入。
        """
        if isinstance(msg, dict):
            for key in ("additional_kwargs", "metadata"):
                holder = msg.get(key)
                if isinstance(holder, dict) and "files" in holder:
                    holder.pop("files")
            return
        for holder_name in ("additional_kwargs", "metadata"):
            holder = getattr(msg, holder_name, None)
            if isinstance(holder, dict) and "files" in holder:
                holder.pop("files")

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
                    f"  沙箱路径: user-data/uploads/{stored_name or filename}\n"
                    f"  (暂无可预览文本，请使用 Read/Bash/Glob 读取该文件)"
                )

        # 调用方传的原始文件名与落盘文件名不一致（源平台把文档导出为
        # 文本/其他格式后存储，落盘名跟随 URL 保证名字与内容一致）时，
        # 在提示词中说明对应关系，模型按实际文件名读取。
        name_note = ""
        if (
            record is not None
            and record.original_name
            and record.original_name != record.stored_name
        ):
            name_note = (
                f"\n  (原始文件名: {record.original_name}；会话内实际文件为 "
                f"user-data/uploads/{record.stored_name}，读取请使用实际文件名)"
            )

        if record and record.is_image:
            # 图片：上传时已固化为 base64（view_image_tool 从元数据直读），
            # 正文不可内联预览，提示 Agent 调用图片解析工具。图片不落沙箱
            # 可读文件，故只给 view_image_tool 的参数 virtual_path（协议
            # 路径，仅该工具可用，勿传给 Read/Bash/Glob）。
            return (
                f"- 文件: {filename} [图片]\n"
                f"  view_image_tool 参数 virtual_path: {virtual_path}\n"
                f"  (图片内容不可内联预览；如需解析图片，请调用 "
                f"view_image_tool 并传入上述 virtual_path 与用户的问题)"
                f"{name_note}"
            )

        if record and record.markdown:
            outline = create_outline_text(record.markdown).strip()
            if outline:
                return (
                    f"- 文件: {filename}\n"
                    f"  沙箱路径: user-data/uploads/{stored_name}\n"
                    f"  大纲/预览:\n{outline}\n"
                    f"  (如需全文，请用 Read/Bash 读取沙箱路径下的原始文件或同名 .md)"
                    f"{name_note}"
                )
        # 无 .md 时仅给文件名 + 沙箱相对路径引用
        return (
            f"- 文件: {filename}\n"
            f"  沙箱路径: user-data/uploads/{stored_name}\n"
            f"  (暂无可预览文本，请使用 Read/Bash/Glob 读取该文件)"
            f"{name_note}"
        )

    @staticmethod
    def _append_to_last_human(messages: list, text: str) -> None:
        """在最后一条 human 消息的副本上追加文本并替换列表引用。

        messages 是 ``_prepare_model_input`` 组装的本次调用快照（列表
        新建，元素引用 ``state.context`` 的消息对象）；在副本上追加、
        替换引用，保证注入不写回 ``agent.state.context``（不持久化）。
        """
        found = UploadsMiddleware._find_last_human(messages)
        if found is None:
            return
        idx, msg = found
        if isinstance(msg, dict):
            copied = dict(msg)
            content = copied.get("content")
            if isinstance(content, str):
                copied["content"] = f"{content}\n\n{text}"
            elif isinstance(content, list):
                copied["content"] = list(content) + [
                    {"type": "text", "text": text},
                ]
        else:
            try:
                copied = msg.model_copy(deep=True)
            except Exception:  # noqa: BLE001 —— 非 pydantic 兜底（原地追加）
                copied = msg
            content = getattr(copied, "content", None)
            if isinstance(content, str):
                copied.content = f"{content}\n\n{text}"
            elif isinstance(content, list):
                # Msg 对象：content 为 ContentBlock 对象列表（新版），
                # 也可能混入 dict（旧版序列化形态），统一追加文本块。
                block = (
                    TextBlock(text=text)
                    if TextBlock is not None
                    else {"type": "text", "text": text}
                )
                copied.content = list(content) + [block]
        messages[idx] = copied


# 模块级实例：MiddlewareRegistry.load_builtin() 会自动扫描并注册，
# 与 LoggingMiddleware 等并列，无需改 factory.py。
uploads_mw = UploadsMiddleware()
