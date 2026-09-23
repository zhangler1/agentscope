# -*- coding: utf-8 -*-
"""Built-in tools — example custom tools for the agent.

Each function here is decorated with ``@tool`` from agentscope so
it gets auto-registered when :meth:`ToolRegistry.load_builtin_tools`
imports this module.

## How to add a new tool

1. Write a function with type hints and a docstring.
2. Decorate it with ``@tool``.
3. The ``ToolRegistry`` will pick it up automatically.

## Custom tools

Put product-specific tools in ``custom/`` to keep built-in tools
clean. The ``custom/`` package is auto-imported if it exists.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ._naming import tool_name

logger = logging.getLogger(__name__)

# 事件日志通道：``as`` logger 自带 events.log 滚动 handler 且经
# main.py ``_EventsFormatter`` 自动注入 trace_id——与 MODEL_*/TOOL_*
# 事件同文件，可按 trace / session 关联图片解析链路。
_events_logger = logging.getLogger("as")

try:
    from agentscope.tool import tool
except ImportError:
    # Fallback: if agentscope.tool is not available, create a no-op
    # decorator so the module still imports for syntax checking.
    def tool(*args, **kwargs):  # type: ignore
        """Fallback @tool decorator when agentscope is not installed."""
        if len(args) == 1 and callable(args[0]):
            fn = args[0]
            fn._is_tool = True  # type: ignore
            return fn

        def decorator(fn):
            fn._is_tool = True  # type: ignore
            return fn

        return decorator


@tool
def get_current_time() -> str:
    """获取当前日期和时间。

    Returns:
        str: 当前日期和时间，ISO 格式。
    """
    from datetime import datetime

    return datetime.now().isoformat()

get_current_time._tool_display_name = tool_name("获取当前时间", "get_current_time")


@tool
def echo(text: str) -> str:
    """将输入文本原样返回给调用方。

    Args:
        text (str): 要回显的文本。

    Returns:
        str: 原样返回的文本。
    """
    return text

echo._tool_display_name = tool_name("回显", "echo")


# ---------------------------------------------------------------------------
# 图片解析工具（配合上传能力：图片上传时已固化为 base64 存于元数据）
# ---------------------------------------------------------------------------
_VISION_ANALYSIS_PROMPT = (
    "你是一个专业的图片分析助手。请根据用户的问题，对图片进行详细分析。\n\n"
    "用户的问题：{question}\n\n"
    "请用中文详细回答，包含以下内容：\n"
    "1. 图片的整体描述\n"
    "2. 与用户问题相关的关键细节\n"
    "3. 图片中的文字内容（如有）"
)

# 工具运行时依赖（main.py 经 set_tool_runtime_deps 注入）：
# 图片解析统一多模态模型经 PG runtime_configs 表 view_image 配置（可经
# /api/config/view_image 热更新）构建时，需要查凭证 / 刷新 ELLM key；
# 未注入依赖时统一模型不可用（工具返回 None）。
_tool_storage: Any = None
_tool_message_bus: Any = None


def set_tool_runtime_deps(storage: Any, message_bus: Any) -> None:
    """注入工具运行时依赖（main.py 启动时调用一次）。

    Args:
        storage: 框架 StorageBase（get_credential / upsert_credential）。
        message_bus: 框架 MessageBus（ELLM key 刷新分布式锁）。
    """
    global _tool_storage, _tool_message_bus
    _tool_storage = storage
    _tool_message_bus = message_bus


async def _get_vision_model():
    """构建图片解析视觉模型（与压缩模型同模式：PG 配置唯一来源）。

    读 PG ``runtime_configs`` 表 ``view_image`` 配置（可经
    /api/config/view_image 热更新）：enabled 且凭证可查时临时构建统一
    多模态模型并注入新鲜 ELLM key；无记录 / 未启用 / 凭证缺失 / 构建
    失败均返回 ``None``（工具提示未配置，不再回退 config.yaml）。

    Returns:
        视觉模型实例（调用方负责用后 ``aclose()``）；不可用返回 ``None``。
    """
    from bocomadp.config import ImageParseConfig
    from bocomadp.runtime_config_store import get_typed_config

    cfg = await get_typed_config("view_image", ImageParseConfig)
    if (
        cfg is not None
        and cfg.enabled
        and cfg.user_id
        and cfg.credential_id
        and cfg.model_name
    ):
        if _tool_storage is None:
            logger.warning(
                "view_image: tool runtime deps not injected; "
                "unified model unavailable",
            )
        else:
            record = await _tool_storage.get_credential(
                cfg.user_id,
                cfg.credential_id,
            )
            if record is None:
                logger.warning(
                    "view_image: credential %r not found for user %r; "
                    "unified model unavailable",
                    cfg.credential_id,
                    cfg.user_id,
                )
            else:
                try:
                    from bocomadp.view_image_model_builder import (
                        build_image_parse_model,
                    )
                    from bocomadp.providers.ellm_chat_model import (
                        EllmChatModel,
                    )
                    from bocomadp.providers.ellm_key import EllmKeyRefresher

                    model = build_image_parse_model(
                        record.data,
                        cfg.model_name,
                    )
                    # 图片解析调用不走 on_model_call 链，必须在此主动保证
                    # key 新鲜（ensure_fresh_key 惰性刷新，有效则零开销）；
                    # 401 双回调仍保留作兜底（见 providers/ellm_chat_model.py）。
                    if (
                        isinstance(model, EllmChatModel)
                        and _tool_message_bus is not None
                    ):
                        refresher = EllmKeyRefresher(
                            _tool_storage,
                            _tool_message_bus,
                            cfg.user_id,
                        )
                        key, _ = await refresher.ensure_fresh_key(
                            cfg.credential_id,
                        )
                        model.set_api_key(key)
                        model.set_refresh_key_callback(
                            lambda: refresher.force_refresh_key(
                                cfg.credential_id,
                            ),
                        )
                        model.set_auth_invalidate_callback(
                            lambda: refresher.invalidate_key(
                                cfg.credential_id,
                            ),
                        )
                    _events_logger.info(
                        "VIEW_IMAGE_MODEL_BUILT provider_id=%s model_name=%s",
                        "view_image",
                        cfg.model_name,
                    )
                    return model
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "view_image: unified model build failed",
                    )
                    _events_logger.exception(
                        "VIEW_IMAGE_MODEL_BUILT_ERROR error=%s",
                        exc,
                    )
    # 无可用统一模型：返回 None，由调用方提示经 /api/config/view_image 配置。
    return None


@tool
async def view_image_tool(
    virtual_path: str = "",
    question: str = "请详细描述这张图片的内容",
    user_id: str = "",
    session_id: str = "",
    agent_id: str = "",
) -> str:
    """读取上传的图片并用多模态模型进行分析。

    适用场景：用户上传了图片（jpg/jpeg/png/webp）并需要解析图片内容时——
    这是解析用户上传图片**唯一**正确的方式。上传时图片已转 base64 固化到
    上传元数据（host 侧 SQLite），本工具直接读取并调用经
    /api/config/view_image 配置的统一多模态模型进行分析——不触碰沙箱文件
    系统，也不依赖主对话模型的多模态能力。

    何时使用 图片解析 工具（必须使用）：
    - 用户询问图片内容 / 图片中的文字 / 图表数据时，无论图片是否已转换。
    - ``<context name="files">`` 中标注为 [图片] 的文件
      （取该行给出的 virtual_path 传入本工具）。
    - 工作区 ``user-data/uploads/`` 下由用户上传的图片文件：从
      <context name="files"> 获取 virtual_path，再调用本工具。

    何时不使用 图片解析 工具：
    - 非图片文件（请改用 Read / Bash 工具直接读取文件）。
    - 仅当图片**不是**通过 /files/upload 接口上传（无固化元数据）时本工具
      无法读取——此时应告知用户通过前端重新上传该图片，而不是用
      Read/bash 直接读取二进制（会得到乱码）。

    Args:
        virtual_path (str): <context name="files"> 中列出的
            virtual_path，例如 /workspace/user-data/uploads/photo.png。
        question (str): 想了解的图片问题或方面，默认一般性描述。
        user_id (str): 租户 id（与上传时一致）。当前会话下可留空，由框架
            自动注入（ChatRunRegistry 派生 run 时拷贝的 ContextVar）。
        session_id (str): 会话 id（与上传时一致）。当前会话下可留空，由框架
            自动注入。
        agent_id (str): agent id（与上传时一致）。空串时仅按 user/session
            过滤。

    Returns:
        str: 图片分析结果文本；失败时返回错误说明。
    """
    from bocomadp.uploads.db import get_uploads_db
    from bocomadp.uploads.manager import resolve_upload_parts

    # 当前会话上下文自动注入（与 build_agent_tools 设置的 ContextVar
    # 一致；显式传入的参数优先）。
    if not user_id or not session_id:
        try:
            from bocomadp.tools.agent_factory_tools import (
                resolve_session_context,
            )

            user_id, session_id = resolve_session_context(user_id, session_id)
        except Exception:  # noqa: BLE001
            pass

    if not user_id or not session_id:
        return (
            "缺少 user_id / session_id。请直接传入当前会话的这两个值"
            "（框架通常会自动注入），以便唯一定位上传记录。"
        )

    # 事件公共上下文段 + 计时起点：所有 VIEW_IMAGE_* 事件共用。
    ctx = f"user_id={user_id} session_id={session_id} agent_id={agent_id}"
    t0 = time.monotonic()

    try:
        _, _, filename = resolve_upload_parts(virtual_path)
    except Exception as exc:  # noqa: BLE001
        _events_logger.error(
            "VIEW_IMAGE_ERROR %s virtual_path=%s error=路径解析失败: %s",
            ctx,
            virtual_path,
            exc,
        )
        return f"路径解析失败（可能越权或非法）: {exc}"

    _events_logger.info(
        "VIEW_IMAGE_INPUT %s virtual_path=%s filename=%s question=%s",
        ctx,
        virtual_path,
        filename,
        (question or "请详细描述这张图片的内容")[:200],
    )

    rec = get_uploads_db().get_by_session_file(
        user_id, session_id, filename, agent_id,
    )
    if rec is None:
        _events_logger.error(
            "VIEW_IMAGE_ERROR %s virtual_path=%s filename=%s "
            "error=上传记录不存在",
            ctx,
            virtual_path,
            filename,
        )
        return f"上传记录不存在: {virtual_path}"
    if not rec.is_image:
        _events_logger.error(
            "VIEW_IMAGE_ERROR %s filename=%s mime_type=%s "
            "error=不是可解析的图片",
            ctx,
            filename,
            getattr(rec, "mime_type", "-"),
        )
        return (
            f"{filename} 不是可解析的图片（支持格式：jpg/jpeg/png/webp，"
            "且需已通过 /files/upload 上传并完成 base64 固化）。"
        )

    vision_model = await _get_vision_model()
    if vision_model is None:
        _events_logger.error(
            "VIEW_IMAGE_ERROR %s filename=%s error=未找到可用的多模态模型",
            ctx,
            filename,
        )
        return (
            "未找到可用的多模态模型：请经 /api/config/view_image 配置统一"
            "多模态模型（PUT /api/config/view_image，字段：enabled / "
            "user_id / credential_id / model_name，enabled=true 时三者必填）"
            "后重试。"
        )
    
    try:
        prompt = _VISION_ANALYSIS_PROMPT.format(
            question=question or "请详细描述这张图片的内容",
        )
        try:
            response = await vision_model.client.chat.completions.create(
                model=vision_model.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{rec.mime_type};base64,{rec.base64}",
                                },
                            },
                        ],
                    },
                ],
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("view_image_tool: vision model call failed")
            return f"多模态模型调用失败: {exc}"
    
        try:
            text = response.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            text = ""
        if not text.strip():
            return f"多模态模型未返回有效内容（{filename}）。"
        logger.info(
            "view_image_tool: analyzed %s (%s), result length=%d",
            virtual_path,
            rec.mime_type,
            len(text),
        )
        _events_logger.info(
            "VIEW_IMAGE_OUTPUT %s filename=%s mime_type=%s cost_ms=%d "
            "result_len=%d",
            ctx,
            filename,
            getattr(rec, "mime_type", "-"),
            int((time.monotonic() - t0) * 1000),
            len(text),
        )
        return f"图片分析结果 ({filename}):\n\n{text}"
    finally:
        # 统一多模态模型每次调用临时构建：用后释放连接池（压缩模型同模式）。
        close = getattr(vision_model, "aclose", None)
        if close is not None:
            await close()

view_image_tool._tool_display_name = tool_name("图片解析", "view_image_tool")