# -*- coding: utf-8 -*-
"""行内搜索工具（vector_search）。

对齐源项目 ``deerflow.community.vector_search.tools``：请求体的
sourceType / repository / aggRepositories / HNSSParam 运行时从
custom_params（ContextVar，来自 Redis 回退）的 ``tools_param.source_param``
读取；source_param 缺失时显式返回错误（源项目会 AttributeError 被兜底，
这里显式处理）。配置仅保留确认的 6 项（api_url / timeout / page_size /
text_top_n / vector_top_n / space_codes）；其余 13 项固定为代码常量
（trans_process/tran_id 空串、headers 默认、cookies 空），不进请求体。

响应归一化 19 字段（title/url/docId/docGuid/score/repository/content/
question/sourceType/knowType/createTime/updateTime/hobbies/
fullCategoryName/orgId/fullOrgName/knowStatus/attachEcmId/fromAttachment）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

try:
    from agentscope.tool import FunctionTool, ToolMiddlewareBase
except ImportError:
    FunctionTool = ToolMiddlewareBase = None

from ..config.vector_search_config import get_vector_search_config
from ._naming import tool_name
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers
from ..deerflow.custom_params import get_custom_params

logger = logging.getLogger(__name__)

#: 固定请求头（对齐源项目 DEFAULT_HEADERS；13 项非配置常量之一）。
_DEFAULT_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "User-Agent": "DeerFlow-VectorSearch/2.0",
}


def _get_source_param() -> dict[str, Any]:
    """从 custom_params 读取 tools_param.source_param（缺失 → 空 dict）。"""
    params = get_custom_params()
    tools_param = (
        (params.get("tools_param") or {}) if isinstance(params, dict) else {}
    )
    source_param = tools_param.get("source_param") or {}
    return source_param if isinstance(source_param, dict) else {}


def _build_request_body(keyword: str) -> dict[str, Any]:
    source_param = _get_source_param()
    body: dict[str, Any] = {
        "REQ_HEAD": {"TRANS_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {
            "param": {
                "sourceType": source_param.get("sourceType"),
                "summaryQuestion": keyword,
                "repository": source_param.get("repository"),
                "aggRepositories": source_param.get("aggRepositories"),
                "param": source_param.get("HNSSParam"),
            },
        },
    }
    return attach_muwp_user(body)


def _extract_entry_info(entry: dict[str, Any]) -> dict[str, Any]:
    title = str(entry.get("title") or "无标题")
    content = str(entry.get("content") or entry.get("absContent") or "")
    score_raw = entry.get("score")
    try:
        score = float(score_raw) if score_raw not in (None, "") else None
    except (TypeError, ValueError):
        score = None
    return {
        "title": title,
        "url": str(entry.get("url") or ""),
        "docId": str(entry.get("docId") or ""),
        "score": score,
        "repository": str(entry.get("repository") or ""),
        "content": content,
        "docGuid": str(entry.get("docGuid") or ""),
        "question": str(entry.get("question") or ""),
        "sourceType": str(entry.get("sourceType") or ""),
        "knowType": str(entry.get("knowType") or ""),
        "createTime": str(entry.get("createTime") or ""),
        "updateTime": str(entry.get("updateTime") or ""),
        "hobbies": entry.get("hobbies", []),
        "fullCategoryName": entry.get("fullCategoryName", []),
        "orgId": str(entry.get("orgId") or ""),
        "fullOrgName": str(entry.get("fullOrgName") or ""),
        "knowStatus": str(entry.get("knowStatus") or ""),
        "attachEcmId": str(entry.get("attachEcmId") or ""),
        "fromAttachment": bool(entry.get("fromAttachment", False)),
    }


def _extract_results(payload: dict[str, Any], keyword: str) -> list[dict[str, Any]]:
    response_head = payload.get("RSP_HEAD", {})
    if response_head and response_head.get("TRAN_SUCCESS") != "1":
        # 用户约束：非成功时以 debug 级别打印完整原始响应
        logger.debug("vector_search origin response: %s", payload)
        return [
            {
                "error": (
                    f"API返回错误，核心状态码:"
                    f"{response_head.get('PROCESS_STATUS_CODE', '未知错误')};"
                    f"错误信息:{response_head.get('ERROR_MESSAGE', '未知错误')}"
                )
            }
        ]

    all_entries = payload.get("RSP_BODY", {}).get("result", [])
    if not isinstance(all_entries, list):
        logger.warning(
            "Vector search returned unexpected result payload: %s",
            all_entries,
        )
        all_entries = []
    if not all_entries:
        return [{"info": f"未找到相关内容。关键词: {keyword}"}]
    return [_extract_entry_info(entry) for entry in all_entries]


async def search_vector_backend(keyword: str) -> str:
    """执行 vector search 后端请求（JSON POST）。"""
    config = get_vector_search_config()
    if not config.api_url:
        raise ValueError(
            "VECTOR_SEARCH_API_URL is required. Set it in config.yaml "
            "or the environment."
        )

    source_param = _get_source_param()
    if not source_param:
        # 源项目此处 AttributeError 被兜底为通用错误；这里显式给出清晰错误
        return json.dumps(
            [{"error": "缺少 custom_params.tools_param.source_param 配置，"
                       "无法构建行内搜索请求。"}],
            ensure_ascii=False,
        )

    headers = dict(_DEFAULT_HEADERS)
    build_auth_headers(headers)

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        response = await client.post(
            config.api_url,
            headers=headers,
            json=_build_request_body(keyword),
        )
        response.raise_for_status()

    logger.debug("vector_search raw response body: %s", response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"vector search returned invalid JSON: {exc}") from exc

    results = _extract_results(payload, keyword)
    return json.dumps(results, indent=2, ensure_ascii=False)


async def _vector_search_tool_impl(keyword: str) -> str:
    """行内搜索

    用户请求涉及知识库内的交行内部信息，包含内部政策、产品、流程、FAQ、
    业务知识、人事任免、履历、任职情况、规章制度、交银办系列文号文件等
    内容时，优先使用本工具。

    Args:
        keyword: 用于检索的输入文本，可传入用户问题、完整语句或关键词。
    """
    try:
        return await search_vector_backend(keyword)
    except httpx.TimeoutException:
        logger.error("Vector search request timed out.", exc_info=True)
        return json.dumps(
            [{"error": "vector search request timed out."}],
            ensure_ascii=False,
        )
    except httpx.HTTPError as exc:
        logger.error("Vector search request failed: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"vector search request failed: {exc}"}],
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        logger.error("Unexpected vector search error: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"vector search failed: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None and ToolMiddlewareBase is not None:
    vector_search_tool = FunctionTool(
        _vector_search_tool_impl,
        # 工具名（按用户要求中文化）
        # 行外 DeepSeek 等 API 强校验：BOCOMADP_TOOL_ASCII_NAMES=1 切英文。
        name=tool_name("行内搜索", "vector_search"),
        is_read_only=True,
    )
else:
    vector_search_tool = _vector_search_tool_impl


__all__ = [
    "search_vector_backend",
    "vector_search_tool",
]
