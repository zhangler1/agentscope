# -*- coding: utf-8 -*-
"""联网搜索工具（online_search，合并 custom_search 后端逻辑）。

对齐源项目 ``deerflow.community.online_search.tools`` 与
``deerflow.community.custom_search.tools``：online_search 本身就是
custom_search 后端的薄包装（repository 写死 ``online-search``），
迁移时把请求构建 / 响应解析逻辑并入本模块，不拆成两个工具。

请求体：REQ_HEAD(TRANS_PROCESS/TRAN_ID 空) + REQ_BODY.param{
summaryQuestion, repository="online-search", param{channelId="0"}}；
muwpUser 仅在 muwp-user 认证模式下附加。响应要求 TRAN_SUCCESS=="1"，
归一化为 8 字段（title/url/content/score/source/createTime/repository/
question），按 score 降序截断 max_results。

本工具在主服务进程内执行（FunctionTool 直接调用），ContextVar 与
Redis 存储（custom_params）均可读。
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

from ..config.online_search_config import get_online_search_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers

logger = logging.getLogger(__name__)

#: online_search 固定使用的 repository（源项目写死常量）。
_DEFAULT_ONLINE_SEARCH_REPOSITORY = "online-search"


def _build_request_body(query: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "REQ_HEAD": {"TRANS_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {
            "param": {
                "summaryQuestion": query,
                "repository": _DEFAULT_ONLINE_SEARCH_REPOSITORY,
                "param": {"channelId": "0"},
            },
        },
    }
    return attach_muwp_user(body)


def _parse_response(payload: dict[str, Any], max_results: int) -> list[dict[str, Any]]:
    response_head = payload.get("RSP_HEAD", {})
    if response_head.get("TRAN_SUCCESS") != "1":
        # 用户约束：非成功时以 debug 级别打印完整原始响应，便于排查网关异常
        logger.debug("online_search origin response: %s", payload)
        return []

    raw_results = payload.get("RSP_BODY", {}).get("result", [])
    normalized: list[dict[str, Any]] = []
    for item in raw_results:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "") or item.get("absContent", "")).strip()
        if not title and not content:
            continue
        score_raw = item.get("score")
        try:
            score = float(score_raw) if score_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            score = 0.0
        normalized.append(
            {
                "title": title,
                "url": str(item.get("url") or ""),
                "content": content,
                "score": score,
                "source": str(item.get("source", "")),
                "createTime": str(item.get("createTime", "")),
                "repository": str(item.get("repository", "")),
                "question": str(item.get("question", "")),
            }
        )
    normalized.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    return normalized[:max_results]


async def search_online_backend(query: str) -> list[dict[str, Any]]:
    """执行联网搜索后端请求（测试直接调用）。"""
    config = get_online_search_config()
    if not config.api_url:
        raise ValueError(
            "CUSTOM_SEARCH_API_URL is required. Set it in config.yaml "
            "or the environment."
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "User-Agent": "DeerFlow-CustomSearch/2.0",
    }
    build_auth_headers(headers)

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        response = await client.post(
            config.api_url,
            headers=headers,
            json=_build_request_body(query),
        )
        response.raise_for_status()

    logger.debug("online_search raw response body: %s", response.text)
    return _parse_response(response.json(), config.max_results)


async def _online_search_tool_impl(query: str) -> str:
    """联网搜索

    搜索公共互联网信息。

    Args:
        query: 搜索使用的"提问"。
    """
    try:
        results = await search_online_backend(query)
    except httpx.TimeoutException as exc:
        logger.error("Online search request timed out: %s", exc, exc_info=True)
        return json.dumps([{"error": "联网搜索请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("Online search request failed: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"联网搜索请求失败: {exc}"}],
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        logger.error("Unexpected online search error: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"联网搜索失败: {exc}"}],
            ensure_ascii=False,
        )

    return json.dumps(results, indent=2, ensure_ascii=False)


if FunctionTool is not None and ToolMiddlewareBase is not None:
    online_search_tool = FunctionTool(
        _online_search_tool_impl,
        # 工具函数名必须是 ^[a-zA-Z0-9_-]+$（DeepSeek 等 API 强校验）
        # [对比测试临时改动] 行外 deepseek-chat 联调：改回英文名。
        name="online_search",
        is_read_only=True,
    )
else:
    # agentscope 不可用时的降级：保持裸函数（与 cross_search.py 风格一致）
    online_search_tool = _online_search_tool_impl


__all__ = [
    "online_search_tool",
    "search_online_backend",
]
