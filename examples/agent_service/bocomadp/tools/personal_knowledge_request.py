# -*- coding: utf-8 -*-
"""个人知识库全文检索工具（personal_knowledge_request，从 deerflow 迁移）。

【迁移改动】（对齐 raw_request.py 迁移先例，业务逻辑不变）
- 去掉 langchain ``@tool`` 装饰器：``FunctionTool`` 根据函数签名 / docstring
  自动生成工具名、描述与参数 schema；
- ``requests`` → ``httpx``（项目已有依赖），函数改为 async，避免阻塞
  run 任务的事件循环；
- 认证 / custom_params 直接使用 bocomadp 已迁移的上下文模块
  （``deerflow/auth_context.py`` 的 :func:`build_auth_headers` /
  :func:`attach_user_identity` 与源文件手写的请求头 / 用户标识注入逐行
  等价；``deerflow/custom_params.py``）；
- 文件下载：deer-flow ``download_urls_to_thread(thread_id, request_id,
  [oss_url])`` → ``download_urls_to_session(user_id, agent_id, session_id,
  [oss_url], storage, workspace_manager)``（bocomadp 语义对齐实现：
  thread_id == session_id，落盘 / 图片固化 / 文档转 .md / uploads DB
  记录一应俱全，下游 ``read_tool_result`` 等立即可见）。
  ``user_id`` / ``agent_id`` / ``session_id`` 由 :func:`build_enterprise_tools`
  经 ContextVar 注入（对齐 cross_search 模式）；``storage`` /
  ``workspace_manager`` 经 ``routers/uploads.py`` 的
  :func:`set_upload_dependencies` 由 main.py 启动时注册；
- ``thread_id`` 读取顺序：源逻辑从 custom_params 取（入参仅占位，源代码
  即如此），bocomadp 适配为 custom_params 未携带时回退当前会话 ID
  （deerflow 通道 thread_id == session_id）；
- 联机地址外置：源文件 ``API_URL_MAP`` 的两个完整 URL 拆为「根地址
  （``config.yaml`` 的 ``personal_knowledge_request`` 节点，见
  ``config/personal_knowledge_request_config.py``，域名跨环境可切）+
  接口路径表（本文件 ``DEFAULT_API_PATHS``，契约不变）」，请求超时
  同节点可配（对齐 raw_request 先例）；
- 去掉源文件调试遗留日志 ``zxl_result``。

【接口说明】（源逻辑原样；挂载无条件，参数缺失时调用报明确错误）
- ``knowledge_query``：按 ``psnlCategoryIdList``（custom_params 的
  ``tools_param.personalKnowledgeSearch``）分页查询个人知识库文档列表，
  多分类自动聚合；分类列表缺失时返回错误提示（挂载不依赖参数齐备，
  可用性由 ``usableTools`` 过滤控制）；
- ``knowledge_file_link``：按 ``fileId`` 获取文件分享链接，默认将文件
  下载到当前会话 uploads 目录（``download_files=True``）。
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

import httpx

try:
    from agentscope.tool import FunctionTool, ToolMiddlewareBase
except ImportError:
    FunctionTool = ToolMiddlewareBase = None

from ..config.personal_knowledge_request_config import (
    get_personal_knowledge_request_config,
)
from ..deerflow.auth_context import attach_user_identity, build_auth_headers
from ..deerflow.custom_params import get_custom_params
from ..routers.uploads import download_urls_to_session, get_upload_dependencies
from ._naming import tool_name

logger = logging.getLogger(__name__)

# 会话标识注入（对齐 cross_search：build_enterprise_tools 在 run 任务内
# set，工具调用时读取；ContextVar 随上下文传播，跨工具调用稳定）。
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "personal_knowledge_user_id",
    default="",
)
_current_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "personal_knowledge_agent_id",
    default="",
)
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "personal_knowledge_session_id",
    default="",
)

# 接口标识 → 联机路径（接口契约，原工程原样保留）；
# 根地址由 config/personal_knowledge_request_config.py 提供（域名跨环境可切）。
DEFAULT_API_PATHS: dict[str, str] = {
    "knowledge_query": "/EUVD.EUVD-OPENAPI.V-1.0/queryKnowledgeByPage.do",
    "knowledge_file_link": "/EUVD.EUVD-OPENAPI.V-1.0/getPersonFileLink.do",
}


def _get_knowledge_params() -> tuple[str | None, Any]:
    """从 custom_params 读取用户编码与个人知识分类列表。

    挂载不再要求参数齐备（无条件构建，可用性由 ``usableTools`` 控制），
    取值链做 None 防御：结构缺失时对应返回 ``None``，由调用方前置校验
    给出明确错误。
    """
    custom_params = get_custom_params() or {}
    tools_param = custom_params.get("tools_param") or {}
    personal_knowledge_search = (
        tools_param.get("personalKnowledgeSearch") or {}
    )
    user_code = custom_params.get("userCode")
    category_id = personal_knowledge_search.get("psnlCategoryIdList")

    return user_code, category_id


def _build_request_body(
    intent: str,
    file_id: str | None = None,
    page_num: int = 1,
    page_size: int = 100,
    category_id: str | None = None,
) -> dict:
    """按接口标识构建请求体（源逻辑原样，用户标识注入复用共享底座）。"""
    user_code, category_id_param = _get_knowledge_params()

    if category_id is None:
        # 防御：挂载不要求参数齐备，列表缺失/为空/非列表时保持 None
        # （knowledge_query 主流程已前置拦截报错；file_link 请求体不含
        # categoryId，None 无害）。
        if isinstance(category_id_param, list) and category_id_param:
            category_id = category_id_param[0]

    if intent == "knowledge_query":
        body: dict = {
            "REQ_HEAD": {
                "TRANS_PROCESS": "",
                "TRAN_ID": ""
            },
            "REQ_BODY": {
                "param": {
                    "sourceSystem": "1010",
                    "userCode": user_code,
                    "categoryId": category_id,
                    "pageNum": page_num,
                    "pageSize": page_size
                }
            }
        }
    elif intent == "knowledge_file_link":
        body = {
            "REQ_HEAD": {
                "TRANS_PROCESS": "",
                "TRAN_ID": ""
            },
            "REQ_BODY": {
                "param": {
                    "sourceSystem": "1010",
                    "userCode": user_code,
                    "fileId": file_id
                }
            }
        }
    else:
        return {}

    # 源文件手写的 muwpUser / guipUser 注入 → attach_user_identity（等价）
    return attach_user_identity(body)


def _get_common_headers() -> dict[str, str]:
    """按当前认证上下文构建请求头（源逻辑原样，复用 build_auth_headers）。"""
    return build_auth_headers({})


def _parse_knowledge_docs(payload: dict) -> list[dict]:
    """解析知识库文档列表（源逻辑原样）。"""
    infos = payload.get("RSP_BODY", {}).get("result", {}).get("infos", [])
    if not infos:
        return []
    return [
        {
            "fileId": doc.get("fileId"),
            "title": doc.get("title"),
            "fileName": doc.get("fileName"),
            "uploadTime": doc.get("uploadTime")
        }
        for doc in infos
    ]


async def _fetch_knowledge_docs(
    category_id: str,
    page_num: int,
    page_size: int,
    url: str,
    headers: dict,
    thread_id: str | None,
    request_id: str | None,
) -> list[dict]:
    """拉取单个分类的文档列表（源逻辑原样，requests → httpx）。"""
    body = _build_request_body(
        "knowledge_query",
        page_num=page_num,
        page_size=page_size,
        category_id=category_id,
    )
    logger.info(
        "[personal_knowledge_request] thread_id=%s, request_id=%s, "
        "categoryId=%s, body=%s",
        thread_id,
        request_id,
        category_id,
        body,
    )
    try:
        async with httpx.AsyncClient(
            timeout=get_personal_knowledge_request_config().timeout,
        ) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error(
            "[personal_knowledge_request] thread_id=%s, request_id=%s, "
            "请求超时: %s",
            thread_id,
            request_id,
            url,
            exc_info=True,
        )
        return []
    except httpx.HTTPError as exc:
        logger.error(
            "[personal_knowledge_request] thread_id=%s, request_id=%s, "
            "请求失败: %s",
            thread_id,
            request_id,
            exc,
            exc_info=True,
        )
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        logger.error(
            "[personal_knowledge_request] thread_id=%s, request_id=%s, "
            "JSON解析失败: %s",
            thread_id,
            request_id,
            exc,
            exc_info=True,
        )
        return []

    return _parse_knowledge_docs(payload)


async def _handle_file_download(
    payload: dict,
    thread_id: str | None = None,
    request_id: str | None = None,
) -> str:
    """下载文件链接到当前会话 uploads 目录（源逻辑原样，下载函数适配）。"""
    # 源逻辑：thread_id 缺失时从 custom_params 回读；bocomadp 适配为回退
    # 当前会话 ID（build_enterprise_tools 注入，thread_id == session_id）。
    if not thread_id:
        thread_id = _current_session_id.get()

    if not thread_id:
        return json.dumps({
            "message": "未提供thread_id，无法下载文件",
            "data": payload
        }, indent=2, ensure_ascii=False)

    file_url = payload.get("RSP_BODY", {}).get("result", {}).get("shareLink", "")

    if not file_url:
        return json.dumps({
            "message": "未获取到文件下载地址",
            "data": payload
        }, indent=2, ensure_ascii=False)

    oss_url = file_url.replace("https://", "http://")

    user_id = _current_user_id.get()
    agent_id = _current_agent_id.get()
    storage, workspace_manager = get_upload_dependencies()
    try:
        # 源：asyncio.run(download_urls_to_thread(thread_id, request_id, [oss_url]))
        # → bocomadp 等价实现（run 任务内已处于事件循环，直接 await）。
        downloaded = await download_urls_to_session(
            user_id,
            agent_id,
            thread_id,
            [oss_url],
            storage,
            workspace_manager,
        )
        return json.dumps({
            "message": f"成功下载 {len(downloaded)} 个文件",
            "downloaded_files": [f.model_dump() for f in downloaded],
            "file_link_response": payload
        }, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 —— 源逻辑：下载失败整体兜底
        logger.error("文件下载失败: %s", exc, exc_info=True)
        return json.dumps([{"error": f"文件下载失败: {exc}"}], ensure_ascii=False)


async def _personal_knowledge_request_tool_impl(
    intent: str,
    file_id: str | None = None,
    page_num: int = 1,
    page_size: int = 100,
    thread_id: str | None = None,
    download_files: bool = True,
) -> str:
    """个人知识库全文检索：根据请求标识自动构建请求体并发送POST请求。

    本工具根据intent标识自动获取对应的URL并构建请求体，发送POST请求并获取返回结果。
    支持知识库查询和文件链接获取两个接口。

    Args:
        intent: 接口标识，支持knowledge_query和knowledge_file_link
        file_id: 文件ID，仅knowledge_file_link需要
        page_num: 页码，仅knowledge_query有效，默认1
        page_size: 每页数量，仅knowledge_query有效，默认100
        thread_id: 线程ID，用于下载文件。如不提供，将从custom_params中尝试获取
        download_files: 是否下载文件到当前线程，默认True（仅knowledge_file_link有效）
    """
    path = DEFAULT_API_PATHS.get(intent)
    if not path:
        valid_intents = ", ".join(DEFAULT_API_PATHS.keys())
        return json.dumps([{"error": f"无效的接口标识: {intent}，支持的标识: {valid_intents}"}], ensure_ascii=False)

    cfg = get_personal_knowledge_request_config()
    url = f"{cfg.api_url.rstrip('/')}{path}"

    if intent == "knowledge_file_link" and not file_id:
        return json.dumps([{"error": "knowledge_file_link需要提供file_id参数"}], ensure_ascii=False)

    headers = _get_common_headers()
    custom_params = get_custom_params()
    # 源逻辑：thread_id / request_id 一律取自 custom_params（入参仅占位）；
    # bocomadp 适配：custom_params 未携带 thread_id 时回退当前会话 ID。
    thread_id = custom_params.get("thread_id") or _current_session_id.get()
    request_id = custom_params.get("requestId")

    # 前置校验：custom_params 关键参数缺失时直接返回明确错误，
    # 不发注定失败的请求（挂载已无条件化，参数完整性由调用时校验保证）。
    user_code, category_ids = _get_knowledge_params()
    if not user_code:
        return json.dumps([{"error": "用户编码缺失：请在 custom_params.userCode 提供用户编码"}], ensure_ascii=False)
    if intent == "knowledge_query" and not category_ids:
        return json.dumps([{"error": "分类参数缺失：请在 tools_param.personalKnowledgeSearch.psnlCategoryIdList 提供个人知识分类列表"}], ensure_ascii=False)

    if intent == "knowledge_query":
        if isinstance(category_ids, list) and len(category_ids) > 1:
            all_docs = []
            for category_id in category_ids:
                docs = await _fetch_knowledge_docs(
                    category_id, page_num, page_size, url, headers,
                    thread_id, request_id,
                )
                all_docs.extend(docs)
            return json.dumps({"documents": all_docs, "total": len(all_docs)}, indent=2, ensure_ascii=False)
        elif isinstance(category_ids, list) and len(category_ids) == 1:
            category_ids = category_ids[0]

        if isinstance(category_ids, list):
            category_ids = category_ids[0] if category_ids else None

    body = _build_request_body(intent, file_id, page_num, page_size)
    if not body:
        return json.dumps([{"error": "请求体构建失败"}], ensure_ascii=False)

    logger.info("[personal_knowledge_request] thread_id=%s, request_id=%s, intent=%s, body=%s", thread_id, request_id, intent, body)
    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("[personal_knowledge_request] thread_id=%s, request_id=%s, 请求超时: %s", thread_id, request_id, url, exc_info=True)
        return json.dumps([{"error": "请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("[personal_knowledge_request] thread_id=%s, request_id=%s, 请求失败: %s", thread_id, request_id, exc, exc_info=True)
        return json.dumps([{"error": f"请求失败: {exc}"}], ensure_ascii=False)

    try:
        payload = response.json()
    except ValueError as exc:
        return json.dumps([{"error": f"请求返回了无效的JSON: {exc}"}], ensure_ascii=False)

    if intent == "knowledge_file_link" and download_files:
        return await _handle_file_download(payload, thread_id, request_id)

    if intent == "knowledge_query":
        docs = _parse_knowledge_docs(payload)
        return json.dumps({"documents": docs, "total": len(docs)}, indent=2, ensure_ascii=False)

    return json.dumps(payload, indent=2, ensure_ascii=False)


if FunctionTool is not None and ToolMiddlewareBase is not None:
    personal_knowledge_request_tool = FunctionTool(
        _personal_knowledge_request_tool_impl,
        # 工具名显式中文化（对齐源 @tool("个人知识库全文检索")）。
        # 行外 DeepSeek 等 API 强校验工具名 ^[a-zA-Z0-9_-]+$，
        # 行外环境设置 BOCOMADP_TOOL_ASCII_NAMES=1 切换英文名。
        name=tool_name("个人知识库全文检索", "personal_knowledge_request"),
        # knowledge_file_link 会向会话 uploads 目录写文件，非只读。
    )
else:
    personal_knowledge_request_tool = _personal_knowledge_request_tool_impl


__all__ = [
    "_current_user_id",
    "_current_agent_id",
    "_current_session_id",
    "personal_knowledge_request_tool",
]
