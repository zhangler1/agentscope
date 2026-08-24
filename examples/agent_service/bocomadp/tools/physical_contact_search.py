# -*- coding: utf-8 -*-
"""物理系统负责人查询工具（physical_contact_search）。

对齐源项目 deer-flow 的物理系统负责人查询实现：application/json
POST 请求，``REQ_HEAD`` + ``REQ_BODY.param``（仅包含非 None 搜索条件），
muwp-user 模式下附加 ``REQ_BODY.muwpUser``。响应取 ``RSP_BODY.result.list``
（物理系统资产条目），归一化为 JSON 返回。

改造点（相对源项目 langchain 实现）：
- ``requests``（同步）→ ``httpx.AsyncClient``（异步，对齐本项目企业工具）；
- ``@tool("物理系统负责人查询", parse_docstring=True)`` → ``FunctionTool``
  显式包装（``is_read_only=True``，查询类工具只读）；
- 认证逻辑收敛到 :func:`build_auth_headers` / :func:`attach_muwp_user`；
- 配置从本项目 ``config.yaml`` 的 ``physical_contact_search`` 节点读取。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

try:
    from agentscope.tool import FunctionTool
except ImportError:
    FunctionTool = None

from ..config.physical_contact_search_config import (
    get_physical_contact_search_config,
)
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers

logger = logging.getLogger(__name__)


def _build_request_body(
    keyword: str | None = None,
) -> dict:
    """构建请求体，仅包含非 None 的搜索参数。"""
    param: dict[str, str] = {}
    if keyword is not None:
        param["keyword"] = keyword
    body = {
        "REQ_HEAD": {
            "TRANS_PROCESS": "",
            "TRAN_ID": "",
        },
        "REQ_BODY": {
            "param": param,
        },
    }
    return attach_muwp_user(body)


def _extract_entry_info(entry: dict[str, Any]) -> dict[str, Any]:
    enSyscoding = str(entry.get("enSyscoding") or "")
    eiStatus = str(entry.get("eiStatus") or "")
    topEngineerName = str(entry.get("topEngineerName") or "")
    eiName = str(entry.get("eiName") or "无名称")
    enFullName = str(entry.get("enFullName") or "")
    eiLabel = str(entry.get("eiLabel") or "")
    eiId = str(entry.get("eiId") or "")
    eiDesc = str(entry.get("eiDesc") or "")
    useRange = str(entry.get("useRange") or "")
    topEngineerId = str(entry.get("topEngineerId") or "")

    subSysInfos = entry.get("subSysInfos", [])
    if isinstance(subSysInfos, list):
        subSysInfos = [_extract_sub_sys_info(sub) for sub in subSysInfos]
    else:
        subSysInfos = []

    busiList = entry.get("busiList", [])
    if isinstance(busiList, list):
        busiList = [_extract_contact_info(b, "busi") for b in busiList]
    else:
        busiList = []

    devList = entry.get("devList", [])
    if isinstance(devList, list):
        devList = [_extract_contact_info(d, "dev") for d in devList]
    else:
        devList = []

    return {
        "系统名称": eiName,
        # "系统ID": eiId,
        "层次分类：999002002001-主系统，999002002003-子系统": enSyscoding,
        "系统状态": eiStatus,
        "英文全称": enFullName,
        "系统简称": eiLabel,
        "资产简介": eiDesc,
        "使用范围": useRange,
        # "总工办负责人ID": topEngineerId,
        "总工办负责人姓名": topEngineerName,
        "下属子系统列表": subSysInfos,
        "业务负责人列表": busiList,
        "开发负责人列表": devList,
    }


def _extract_sub_sys_info(sub: dict[str, Any]) -> dict[str, Any]:
    return {
        "系统名称": str(sub.get("eiName") or ""),
        # "系统ID": str(sub.get("eiId") or ""),
        "系统类型": str(sub.get("enSyscoding") or ""),
        "系统状态": str(sub.get("eiStatus") or ""),
        "英文全称": str(sub.get("enFullName") or ""),
        "系统简称": str(sub.get("eiLabel") or ""),
        "系统简介": str(sub.get("eiDesc") or ""),
        "使用范围": str(sub.get("useRange") or ""),
        "开发二级部名称": str(sub.get("devDeptName") or ""),
        # "开发二级部ID": str(sub.get("devDeptId") or ""),
        "开发一级部名称": str(sub.get("devOrgName") or ""),
        # "开发一级部ID": str(sub.get("devOrgId") or ""),
        # "开发负责人ID": str(sub.get("devUserId") or ""),
        "开发负责人名称": str(sub.get("devUserName") or ""),
        # "业务负责人ID": str(sub.get("busiUserId") or ""),
        "业务负责人名称": str(sub.get("busiUserName") or ""),
        # "业务二级部ID": str(sub.get("busiDeptId") or ""),
        "业务二级部名称": str(sub.get("busiDeptName") or ""),
        # "业务一级部ID": str(sub.get("busiOrgId") or ""),
        "业务一级部名称": str(sub.get("busiOrgName") or ""),
        # "总工办负责人ID": str(sub.get("topEngineerId") or ""),
        "总工办负责人名称": str(sub.get("topEngineerName") or ""),
    }


def _extract_contact_info(contact: dict[str, Any], contact_type: str) -> dict[str, Any]:
    if contact_type == "busi":
        return {
            # "业务负责人ID": str(contact.get("busiUserId") or ""),
            "业务负责人名称": str(contact.get("busiUserName") or ""),
            # "业务二级部ID": str(contact.get("busiDeptId") or ""),
            "业务二级部名称": str(contact.get("busiDeptName") or ""),
            # "业务一级部ID": str(contact.get("busiOrgId") or ""),
            "业务一级部名称": str(contact.get("busiOrgName") or ""),
            "电话": str(contact.get("telephone") or ""),
            "是否主负责部门": str(contact.get("isMain") or ""),
            "分摊比例": str(contact.get("percent") or ""),
        }
    else:
        return {
            # "开发人员ID": str(contact.get("devUserId") or ""),
            "开发人员名称": str(contact.get("devUserName") or ""),
            # "开发二级部ID": str(contact.get("devDeptId") or ""),
            "开发二级部名称": str(contact.get("devDeptName") or ""),
            # "开发一级部ID": str(contact.get("devOrgId") or ""),
            "开发一级部名称": str(contact.get("devOrgName") or ""),
            "电话": str(contact.get("telephone") or ""),
            "邮箱": str(contact.get("email") or ""),
            "是否主负责人": str(contact.get("isMain") or ""),
        }


def _extract_results(payload: dict[str, Any], keyword: str) -> list[dict[str, Any]]:
    response_head = payload.get("RSP_HEAD", {})
    logger.debug("physical_search_contact origin response TRACE_NO: %s", response_head.get("TRACE_NO", ""))
    if response_head and response_head.get("TRAN_SUCCESS") != "1":
        return [{"error": "API返回错误，核心状态码:{};"
                          "错误信息:{}".format(
                              response_head.get("PROCESS_STATUS_CODE", "未知错误"),
                              response_head.get("ERROR_MESSAGE", "未知错误"),
                          )}]

    rsp_body = payload.get("RSP_BODY", {})

    all_entries = rsp_body.get("result", {}).get("list", [])
    if not isinstance(all_entries, list):
        logger.warning("physical_search_contact returned unexpected result payload: %s", all_entries)
        all_entries = []

    if not all_entries:
        return [{"info": "未找到相关内容。关键词: {}".format(keyword)}]

    return [_extract_entry_info(entry) for entry in all_entries]


async def physical_search_contact_backend(
    keyword: str | None = None,
) -> str:
    config = get_physical_contact_search_config()
    if not config.api_url:
        raise ValueError(
            "物理系统负责人查询 API URL 未配置。请在 config.yaml 或环境变量 "
            "PHYSICAL_CONTACT_SEARCH_API_URL 中设置。"
        )

    body = _build_request_body(
        keyword=keyword,
    )

    # 检查 REQ_BODY.param 是否为空
    if not body["REQ_BODY"]["param"]:
        raise ValueError("至少需要传入一个搜索参数。")

    headers = dict(config.headers)
    build_auth_headers(headers)

    async with httpx.AsyncClient(timeout=config.timeout) as client:
        response = await client.post(
            config.api_url,
            headers=headers,
            json=body,
        )
        response.raise_for_status()

    logger.debug("Physical contact search raw response body: %s", response.text)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"物理系统负责人查询返回了无效的 JSON: {exc}") from exc

    results = _extract_results(payload, keyword or "")
    return json.dumps(results, indent=2, ensure_ascii=False)


async def _physical_contact_search_tool_impl(
    keyword: str | None = None,
) -> str:
    """物理系统负责人查询

    查询行内物理系统对应负责人联系方式。

    使用场景：需要获取任意物理系统负责人时，优先调用本工具。

    Args:
        keyword: 物理系统检索关键词，支持系统名称
    """
    try:
        return await physical_search_contact_backend(
            keyword=keyword,
        )
    except httpx.TimeoutException:
        logger.error("物理系统负责人查询请求超时。", exc_info=True)
        return json.dumps([{"error": "物理系统负责人查询请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("物理系统负责人查询请求失败: %s", exc, exc_info=True)
        return json.dumps([{"error": f"物理系统负责人查询请求失败: {exc}"}], ensure_ascii=False)
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        logger.error("物理系统负责人查询未知错误: %s", exc, exc_info=True)
        return json.dumps([{"error": f"物理系统负责人查询失败: {exc}"}], ensure_ascii=False)


if FunctionTool is not None:
    physical_contact_search_tool = FunctionTool(
        _physical_contact_search_tool_impl,
        name="physical_contact_search",
        is_read_only=True,
    )
else:
    physical_contact_search_tool = _physical_contact_search_tool_impl


__all__ = [
    "physical_contact_search_tool",
    "physical_search_contact_backend",
]
