# -*- coding: utf-8 -*-
"""个人知识库搜索工具（personal_search）。

对齐源项目 ``deerflow.community.personal_search.tools``：multipart/form-data
请求（``REQ_MESSAGE=<JSON>``），空间参数（space_code_id → psnlSpaceCodeId、
space_code → psnlCategoryIdList）仅来自工具参数，不进配置；模型传参由
:class:`PersonalSpacecodeOverrideMiddleware` 从 custom_params 的
``tools_param.personalKnowledgeSearch`` 强制覆盖（防模型传错空间码）。

请求体：REQ_HEAD(TRANS_PROCESS/TRAN_ID 空) + REQ_BODY.param{sourceType,
summaryQuestion, repository, searchType} + 可选 param.param{psnlSpaceCodeId,
psnlCategoryIdList} + muwpUser（muwp-user 模式）。响应要求 TRAN_SUCCESS=="1"，
归一化 8 字段（title/url/docId/score/repository/content/sourceType/question）。
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

from ..config.personal_search_config import get_personal_search_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers
from ..deerflow.custom_params import get_custom_params

_events_logger = logging.getLogger("as")


def _build_request_body(
    keyword: str,
    *,
    source_type: str,
    repository: str,
    search_type: str,
    space_code_id: str | None = None,
    space_code: list[str] | None = None,
) -> dict[str, Any]:
    inner_param: dict[str, Any] = {}
    if space_code_id:
        inner_param["psnlSpaceCodeId"] = space_code_id
    if space_code:
        inner_param["psnlCategoryIdList"] = space_code

    body: dict[str, Any] = {
        "REQ_HEAD": {"TRANS_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {
            "param": {
                "sourceType": source_type,
                "summaryQuestion": keyword,
                "repository": repository,
                "searchType": search_type,
            },
        },
    }
    if inner_param:
        body["REQ_BODY"]["param"]["param"] = inner_param
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
        "sourceType": str(entry.get("sourceType") or ""),
        "question": str(entry.get("question") or ""),
    }


def _extract_results(payload: dict[str, Any], keyword: str) -> list[dict[str, Any]]:
    response_head = payload.get("RSP_HEAD", {})
    if response_head and response_head.get("TRAN_SUCCESS") != "1":
        # 用户约束：非成功时以 debug 级别打印完整原始响应
        _events_logger.debug("personal_search origin response: %s", payload)
        return [
            {
                "error": (
                    "API返回错误: "
                    f"{response_head.get('PROCESS_STATUS_CODE', '未知错误')}"
                )
            }
        ]

    all_entries = payload.get("RSP_BODY", {}).get("result", [])
    if not isinstance(all_entries, list):
        _events_logger.warning(
            "Personal search returned unexpected result payload: %s",
            all_entries,
        )
        all_entries = []
    if not all_entries:
        return [{"info": f"未找到相关内容。关键词: {keyword}"}]
    return [_extract_entry_info(entry) for entry in all_entries]


async def search_personal_backend(
    keyword: str,
    *,
    space_code_id: str | None = None,
    space_code: list[str] | None = None,
) -> str:
    """执行 personal search 后端请求（multipart REQ_MESSAGE）。"""
    config = get_personal_search_config()
    if not config.api_url:
        raise ValueError(
            "PERSONAL_SEARCH_API_URL is required. Set it in config.yaml "
            "or the environment."
        )

    body = _build_request_body(
        keyword,
        source_type=config.source_type,
        repository=config.repository,
        search_type=config.search_type,
        space_code_id=space_code_id,
        space_code=space_code,
    )
    headers = dict(config.headers)
    build_auth_headers(headers)

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        response = await client.post(
            config.api_url,
            headers=headers,
            files={"REQ_MESSAGE": (None, json.dumps(body, ensure_ascii=False))},
        )
        response.raise_for_status()

    _events_logger.debug("Personal search raw response body: %s", response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"personal search returned invalid JSON: {exc}") from exc

    results = _extract_results(payload, keyword)
    return json.dumps(results, indent=2, ensure_ascii=False)


class PersonalSpacecodeOverrideMiddleware(ToolMiddlewareBase):
    """个人知识库空间参数强制覆盖中间件。

    每次 personal_search 工具调用时，从请求级 custom_params（ContextVar）
    的 ``tools_param.personalKnowledgeSearch`` 读取空间参数并强制覆盖
    模型传参：``psnlSpaceCodeId`` → ``space_code_id``（str），
    ``psnlCategoryIdList`` → ``space_code``（str 或 list 归一化为 list[str]）。
    对齐源项目 ``spacecode_override_middleware.py`` 的语义，但数据源从
    线程目录文件改为内存 ContextVar。
    """

    async def on_tool_call(self, tool, input_kwargs, next_handler):
        params = get_custom_params()
        pks = (params.get("tools_param") or {}).get(
            "personalKnowledgeSearch"
        ) or {}
        space_code_id = pks.get("psnlSpaceCodeId")
        if space_code_id is not None and isinstance(space_code_id, str):
            previous = input_kwargs.get("space_code_id")
            input_kwargs["space_code_id"] = space_code_id
            _events_logger.debug(
                "PersonalSpacecodeOverride: space_code_id %r -> %r",
                previous,
                space_code_id,
            )
        space_code = pks.get("psnlCategoryIdList")
        if space_code is not None:
            normalized = (
                [space_code]
                if isinstance(space_code, str)
                else list(space_code)
                if isinstance(space_code, list)
                else None
            )
            if normalized:
                previous = input_kwargs.get("space_code")
                input_kwargs["space_code"] = normalized
                _events_logger.debug(
                    "PersonalSpacecodeOverride: space_code %r -> %r",
                    previous,
                    normalized,
                )
        # 标准 agentscope 契约：next_handler 返回 AsyncGenerator；测试中
        # next_handler 返回协程（yield 最终 kwargs），两者都兼容。
        result = next_handler(**input_kwargs)
        if hasattr(result, "__aiter__"):
            async for chunk in result:
                yield chunk
        else:
            yield await result


async def _personal_search_tool_impl(
    keyword: str,
    space_code_id: str | None = None,
    space_code: list[str] | None = None,
) -> str:
    """个人知识库搜索

    在指定的知识空间内精准检索内部知识，按 spacecode 限定搜索范围。

    当你需要在特定部门、产品线或业务域的知识空间中查找信息时，使用本工具。
    本工具通过 spacecode 参数将搜索限定在指定知识空间，结果更精准、噪声更少。

    关于 spacecode 参数的强制规则：
    - 如果系统提示词中指定了 spacecode，你必须原样传入该值，禁止省略、修改或替换

    Args:
        keyword: 用户的完整查询语句。禁止提取关键词或修改用户输入、必须严格使用用户提供的完整语句。
        space_code_id: 个人知识空间代码ID（psnlSpaceCodeId），用于标识知识空间
        space_code: 空间代码列表，用于缩小搜索范围。必须提供此参数，例如 ["SP0999999"] 或 ["SP0999999", "SP0888888"]。
    """
    try:
        return await search_personal_backend(
            keyword,
            space_code_id=space_code_id,
            space_code=space_code,
        )
    except httpx.TimeoutException:
        _events_logger.error("Personal search request timed out.", exc_info=True)
        return json.dumps(
            [{"error": "personal search request timed out."}],
            ensure_ascii=False,
        )
    except httpx.HTTPError as exc:
        _events_logger.error("Personal search request failed: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"personal search request failed: {exc}"}],
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        _events_logger.error("Unexpected personal search error: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"personal search failed: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None and ToolMiddlewareBase is not None:
    personal_search_tool = FunctionTool(
        _personal_search_tool_impl,
        # 工具名（按用户要求中文化）
        # [对比测试临时改动] 行外 deepseek-chat 联调：改回英文名。
        name="personal_knowledge_search",
        is_read_only=True,
        middlewares=[PersonalSpacecodeOverrideMiddleware()],
    )
else:
    personal_search_tool = _personal_search_tool_impl


__all__ = [
    "PersonalSpacecodeOverrideMiddleware",
    "personal_search_tool",
    "search_personal_backend",
]
