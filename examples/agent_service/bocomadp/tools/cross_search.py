# -*- coding: utf-8 -*-
"""跨知识搜索工具。

跨场景 / 团队 / 个人知识库进行混合召回搜索，支持全文检索和向量检索。

【迁移说明】
本模块由 ``deerflow`` 的 ``cross_search_tool`` 迁移而来，做了适配：
    - 去掉 ``langchain.tools.tool`` 装饰器：AgentScope 的 ``FunctionTool``
      会自动从函数签名 / docstring 提取工具名、描述与参数 schema；
    - 配置收拢到 ``bocomadp/config/cross_search_config.py`` 模块
      （从 ``config.yaml`` 的 ``cross_search`` 节点提取，字符串值支持
      ``$VAR`` 环境变量引用展开），本模块仅负责使用配置，不再自解析环境变量；
    - 用 ``httpx`` 替代 ``requests``：``httpx`` 是本项目已有依赖，且支持
      multipart ``files`` 上传，接口基本对齐；
    - 安全敏感参数（空间码、用户编码等）为智能体级配置，通过
      ``PUT /agents/{id}/cross-search-config`` 接口设置，存储在 PG
      ``agent_cross_search_configs`` 表，运行时由工具从智能体配置读取，
      不暴露给 LLM。

接入真实环境时，只需配置 ``config.yaml`` 的 ``cross_search.api_url``、
``caller`` / ``user_code``（见 ``config.yaml.example`` 与 ``.env.example``），
或通过智能体配置接口按智能体覆盖。
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

from ..agent_cross_search_config import (
    AgentCrossSearchConfig,
    cross_search_config_get,
)
from ..config.cross_search_config import CrossSearchConfig, get_cross_search_config

logger = logging.getLogger(__name__)

_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cross_search_user_id",
    default="",
)

_current_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "cross_search_agent_id",
    default="",
)


async def _get_agent_cross_search_config() -> AgentCrossSearchConfig | None:
    """从 PG 读取当前智能体的跨知识搜索配置。

    优先级：智能体级配置（PG）> config.yaml 全局默认。
    智能体配置由管理接口 ``PUT /agents/{id}/cross-search-config`` 写入，
    与对话请求无关，属于智能体级别（所有会话共享）。
    """
    user_id = _current_user_id.get()
    agent_id = _current_agent_id.get()
    if not user_id or not agent_id:
        return None
    try:
        return await cross_search_config_get(user_id, agent_id)
    except Exception as exc:
        logger.warning(
            "CrossSearchParams: failed to read agent config for %s/%s: %s",
            user_id,
            agent_id,
            exc,
        )
        return None


def _build_req_message(
    keyword: str,
    config: CrossSearchConfig,
    *,
    agent_config: AgentCrossSearchConfig | None = None,
) -> str:
    if agent_config is not None:
        effective_user_code = agent_config.user_code or config.user_code
        effective_search_type = agent_config.search_type or config.search_type
        effective_space_code_list = (
            agent_config.space_code_list
            if agent_config.space_code_list
            else config.space_code_list
        )
        effective_team_space_code_list = (
            agent_config.team_space_code_list
            if agent_config.team_space_code_list
            else config.team_space_code_list
        )
        effective_psnl_space_code_id = (
            agent_config.psnl_space_code_id or config.psnl_space_code_id
        )
        effective_customized_tag_list = (
            agent_config.customized_tag_list
            if agent_config.customized_tag_list
            else config.customized_tag_list
        )
        effective_psnl_category_id_list = (
            agent_config.psnl_category_id_list
            if agent_config.psnl_category_id_list
            else config.psnl_category_id_list
        )
        effective_text_top_n = (
            agent_config.text_top_n
            if agent_config.text_top_n is not None
            else config.text_top_n
        )
        effective_vector_top_n = (
            agent_config.vector_top_n
            if agent_config.vector_top_n is not None
            else config.vector_top_n
        )
    else:
        effective_user_code = 9501173 # config.user_code
        effective_search_type = config.search_type
        effective_space_code_list = config.space_code_list
        effective_team_space_code_list = config.team_space_code_list
        effective_psnl_space_code_id = config.psnl_space_code_id
        effective_customized_tag_list = config.customized_tag_list
        effective_psnl_category_id_list = config.psnl_category_id_list
        effective_text_top_n = config.text_top_n
        effective_vector_top_n = config.vector_top_n

    if not effective_user_code:
        raise ValueError(
            "userCode is required. 请通过智能体配置接口或在 config.yaml 的 cross_search.user_code 中配置。",
        )

    has_space = bool(
        effective_space_code_list
        or effective_team_space_code_list
        or effective_psnl_space_code_id
    )
    if not has_space:
        raise ValueError(
            "至少需要提供 spaceCodeList、teamSpaceCodeList 或 "
            "psnlSpaceCodeId 中的一个。请通过智能体配置接口设置。",
        )

    param: dict[str, Any] = {
        "keyword": keyword,
        "userCode": effective_user_code,
        "userRole": config.user_role,
        "searchType": effective_search_type,
        "textTopN": effective_text_top_n,
        "vectorTopN": effective_vector_top_n,
        "attachFlag": config.attach_flag,
        "caller": config.caller,
        "rerankFlag": config.rerank_flag,
        "reWriteFlag": config.rewrite_flag,
        "rerankTopN": config.rerank_top_n,
        "rerankRuleCode": config.rerank_rule_code,
        "qaType": config.qa_type,
        "vectorMinScore": (
            config.vector_min_score
            if config.vector_min_score is not None
            else 0
        ),
    }

    if effective_space_code_list:
        param["spaceCodeList"] = effective_space_code_list
    if effective_team_space_code_list:
        param["teamSpaceCodeList"] = effective_team_space_code_list
    if effective_psnl_space_code_id:
        param["psnlSpaceCodeId"] = effective_psnl_space_code_id
    if effective_psnl_category_id_list:
        param["psnlCategoryIdList"] = effective_psnl_category_id_list
    if effective_customized_tag_list:
        param["customizedTagList"] = effective_customized_tag_list
    if config.source_org_id_list:
        param["sourceOrgIdList"] = config.source_org_id_list
    if config.source_system_list:
        param["sourceSystemList"] = config.source_system_list
    if config.text_min_score is not None:
        param["textMinScore"] = config.text_min_score
    if config.pub_time_start:
        param["pubTimeStart"] = config.pub_time_start
    if config.pub_time_end:
        param["pubTimeEnd"] = config.pub_time_end

    req_message = {
        "REQ_HEAD": {
            "TRANS_PROCESS": "searchKnowledgeCross",
            "TRAN_ID": "",
        },
        "REQ_BODY": {
            "param": param,
        },
    }
    return json.dumps(req_message, ensure_ascii=False)


def _extract_entry_info(entry: dict[str, Any], source_type: str) -> dict[str, Any]:
    content = str(entry.get("content") or "")
    score_raw = entry.get("score")
    file_name = str(entry.get("fileName") or "")
    para_title = str(entry.get("paraTitle") or "")
    file_id = str(entry.get("fileId") or "")
    para_id = str(entry.get("paraId") or "")
    pub_time = str(entry.get("pubTime") or "")
    create_time = str(entry.get("createTime") or "")
    update_time = str(entry.get("updateTime") or "")
    kn_type = str(entry.get("knType") or "")
    customized_tags = entry.get("customizedTags") or []
    scene_codes = entry.get("sceneCodes") or []
    source_org_id = str(entry.get("sourceOrgId") or "")
    rerank_score_raw = entry.get("rerankScore")
    valid_time_start = str(entry.get("validTimeStart") or "")
    valid_time_end = str(entry.get("validTimeEnd") or "")
    task_id = str(entry.get("taskId") or "")
    main_task_id = entry.get("mainTaskId") or []
    from_attachment = entry.get("fromAttachment", False)
    domain_tags = entry.get("domainTags") or []
    sorted_val = entry.get("sorted")
    page = entry.get("page")

    try:
        score = float(score_raw) if score_raw not in (None, "") else None
    except (TypeError, ValueError):
        score = None

    result: dict[str, Any] = {
        "sourceType": source_type,
        "content": content,
        "score": score,
        "title": para_title or file_name or "无标题",
        "fileName": file_name,
        "fileId": file_id,
        "paraId": para_id,
        "pubTime": pub_time,
        "createTime": create_time,
        "updateTime": update_time,
        "knType": kn_type,
        "customizedTags": customized_tags,
        "sceneCodes": scene_codes,
        "sourceOrgId": source_org_id,
        "validTimeStart": valid_time_start,
        "validTimeEnd": valid_time_end,
        "taskId": task_id,
        "mainTaskId": main_task_id,
        "fromAttachment": from_attachment,
        "domainTags": domain_tags,
    }

    if sorted_val is not None:
        result["sorted"] = sorted_val
    if page is not None:
        result["page"] = page

    if rerank_score_raw is not None:
        try:
            result["rerankScore"] = float(rerank_score_raw)
        except (TypeError, ValueError):
            pass

    return result


def _extract_results(
    payload: dict[str, Any],
    keyword: str,
) -> list[dict[str, Any]]:
    rsp_body = payload.get("RSP_BODY", payload)
    result_data = rsp_body.get("result", rsp_body)

    all_results: list[dict[str, Any]] = []

    text_list = result_data.get("textGroupList", [])
    if isinstance(text_list, list):
        all_results.extend(_extract_entry_info(e, "text") for e in text_list)

    vector_list = result_data.get("vectorGroupList", [])
    if isinstance(vector_list, list):
        all_results.extend(
            _extract_entry_info(e, "vector") for e in vector_list
        )

    rerank_list = result_data.get("rerankGroupList", [])
    if isinstance(rerank_list, list):
        all_results.extend(_extract_entry_info(e, "rerank") for e in rerank_list)

    if not all_results:
        return [{"info": f"未找到相关内容。关键词: {keyword}"}]

    def _sort_key(r: dict) -> float:
        if "rerankScore" in r:
            return r["rerankScore"]
        return r.get("score") or 0.0

    all_results.sort(key=_sort_key, reverse=True)
    return all_results


async def search_cross_backend(
    keyword: str,
    *,
    agent_config: AgentCrossSearchConfig | None = None,
) -> str:
    config = get_cross_search_config()
    if not config.api_url:
        raise ValueError(
            "cross_search.api_url is required. 请在 config.yaml 中配置。",
        )
    if not config.caller:
        raise ValueError(
            "cross_search.caller is required. 请在 config.yaml 中配置。",
        )

    req_message = _build_req_message(
        keyword,
        config,
        agent_config=agent_config,
    )

    headers = dict(config.headers)
    headers.pop("Content-Type", None)

    files = {"REQ_MESSAGE": (None, req_message)}

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        response = await client.post(
            config.api_url,
            headers=headers,
            files=files,
        )
        response.raise_for_status()

    logger.debug("Cross search raw response body: %s", response.text)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"cross search returned invalid JSON: {exc}") from exc

    rsp_head = payload.get("RSP_HEAD", {})
    if rsp_head.get("TRAN_SUCCESS") != "1":
        process_status = rsp_head.get("PROCESS_STATUS_CODE", "")
        detail = payload.get("RSP_BODY", {}).get("error", "")
        raise ValueError(
            f"cross search failed (status={process_status}): {detail}",
        )

    results = _extract_results(payload, keyword)
    return json.dumps(results, indent=2, ensure_ascii=False)


async def _cross_search_tool_impl(keyword: str) -> str:
    """跨知识搜索

    跨场景、团队、个人知识库进行混合召回搜索，支持全文检索和向量检索。

    当需要在多个知识空间中检索信息、或需要跨不同来源的知识进行综合查询时，
    使用本工具。本工具支持同时搜索场景知识库、团队知识库和个人知识库，
    结果涵盖向量匹配和全文匹配两个维度。

    构造搜索关键词时，应贴近用户原始措辞，提取核心主题词，不要自动添加
    泛化限定词，除非用户明确提及或确实需要消歧。

    空间码、用户编码、检索类型、返回条数等参数由系统自动注入，无需手动传入。

    Args:
        keyword: 用户的完整查询语句。禁止提取关键词或修改用户输入、
            必须严格使用用户提供的完整语句。
    """
    agent_config = await _get_agent_cross_search_config()

    if agent_config is not None:
        logger.info(
            "CrossSearchParams: agent_config loaded for agent=%s, "
            "user_code=%s, space_codes=%s",
            _current_agent_id.get(),
            agent_config.user_code,
            agent_config.space_code_list,
        )

    try:
        return await search_cross_backend(
            keyword,
            agent_config=agent_config,
        )
    except httpx.TimeoutException:
        logger.error("Cross search request timed out.", exc_info=True)
        return json.dumps([{"error": "跨知识搜索请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("Cross search request failed: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"跨知识搜索请求失败: {exc}"}],
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        logger.error("Unexpected cross search error: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"跨知识搜索失败: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None and ToolMiddlewareBase is not None:
    cross_search_tool = FunctionTool(
        _cross_search_tool_impl,
        name="cross_search",
        is_read_only=True,
    )
else:
    cross_search_tool = _cross_search_tool_impl


__all__ = [
    "cross_search_tool",
    "search_cross_backend",
    "_current_user_id",
    "_current_agent_id",
]
