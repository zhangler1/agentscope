# -*- coding: utf-8 -*-
"""通讯录查询工具（contact_search）。

对齐源项目 ``deerflow.community.contact_search.tools``：application/json
POST 请求，``REQ_HEAD`` + ``REQ_BODY.param.toolInput``（仅包含非 None
搜索条件），muwp-user 模式下附加 ``REQ_BODY.muwpUser``。响应取
``RSP_BODY.result`` 列表，归一化为 JSON 返回。

改造点（相对源项目 langchain 实现）：
- ``requests``（同步）→ ``httpx.AsyncClient``（异步，对齐本项目企业工具）；
- ``@tool("通讯录查询", parse_docstring=True)`` → ``FunctionTool``
  显式包装（``is_read_only=True``，查询类工具只读）；
- 认证逻辑收敛到 :func:`build_auth_headers` / :func:`attach_muwp_user`；
- 配置从本项目 ``config.yaml`` 的 ``contact_search`` 节点读取。
"""
from __future__ import annotations

import json
import logging

import httpx

try:
    from agentscope.tool import FunctionTool
except ImportError:
    FunctionTool = None

from ..config.contact_search_config import get_contact_search_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers

logger = logging.getLogger(__name__)

CONTACT_SEARCH_FIELDS = [
    "userName",
    "userIds",
    "orgId",
    "orgIds",
    "orgName",
    "cellPhone",
    "telNo",
    "telNoExt",
    "shortNo",
    "email",
    "ehrPosition",
    "positionStatus",
    "customCellPhone1",
    "loginName",
]


def _build_request_body(
    userName: str | None = None,
    userIds: str | None = None,
    orgId: str | None = None,
    orgIds: str | None = None,
    orgName: str | None = None,
    cellPhone: str | None = None,
    telNo: str | None = None,
    telNoExt: str | None = None,
    shortNo: str | None = None,
    email: str | None = None,
    ehrPosition: str | None = None,
    positionStatus: str | None = None,
    customCellPhone1: str | None = None,
    loginName: str | None = None,
) -> dict:
    """构建请求体，仅包含非 None 的搜索参数。"""
    tool_input: dict[str, str] = {}
    local_vars = {
        "userName": userName,
        "userIds": userIds,
        "orgId": orgId,
        "orgIds": orgIds,
        "orgName": orgName,
        "cellPhone": cellPhone,
        "telNo": telNo,
        "telNoExt": telNoExt,
        "shortNo": shortNo,
        "email": email,
        "ehrPosition": ehrPosition,
        "positionStatus": positionStatus,
        "customCellPhone1": customCellPhone1,
        "loginName": loginName,
    }
    for field in CONTACT_SEARCH_FIELDS:
        value = local_vars[field]
        if value is not None:
            tool_input[field] = value
    body = {
        "REQ_HEAD": {"TRANS_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {"param": {"toolInput": tool_input}},
    }
    return attach_muwp_user(body)


async def search_contact_backend(
    userName: str | None = None,
    userIds: str | None = None,
    orgId: str | None = None,
    orgIds: str | None = None,
    orgName: str | None = None,
    cellPhone: str | None = None,
    telNo: str | None = None,
    telNoExt: str | None = None,
    shortNo: str | None = None,
    email: str | None = None,
    ehrPosition: str | None = None,
    positionStatus: str | None = None,
    customCellPhone1: str | None = None,
    loginName: str | None = None,
) -> str:
    """执行通讯录查询后端请求（application/json POST）。"""
    config = get_contact_search_config()
    if not config.api_url:
        raise ValueError(
            "通讯录查询 API URL 未配置。请在 config.yaml 或环境变量 "
            "CONTACT_SEARCH_API_URL 中设置。"
        )

    body = _build_request_body(
        userName=userName,
        userIds=userIds,
        orgId=orgId,
        orgIds=orgIds,
        orgName=orgName,
        cellPhone=cellPhone,
        telNo=telNo,
        telNoExt=telNoExt,
        shortNo=shortNo,
        email=email,
        ehrPosition=ehrPosition,
        positionStatus=positionStatus,
        customCellPhone1=customCellPhone1,
        loginName=loginName,
    )
    # 检查 toolInput 是否为空
    if not body["REQ_BODY"]["param"]["toolInput"]:
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

    logger.debug("Contact search raw response body: %s", response.text)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"通讯录查询返回了无效的 JSON: {exc}") from exc

    data = payload.get("RSP_BODY", {}).get("result", [])
    if not isinstance(data, list):
        logger.warning(
            "Contact search returned unexpected result payload: %s",
            data,
        )
        data = []
    return json.dumps(data, indent=2, ensure_ascii=False)


async def _contact_search_tool_impl(
    userName: str | None = None,
    userIds: str | None = None,
    orgId: str | None = None,
    orgIds: str | None = None,
    orgName: str | None = None,
    cellPhone: str | None = None,
    telNo: str | None = None,
    telNoExt: str | None = None,
    shortNo: str | None = None,
    email: str | None = None,
    ehrPosition: str | None = None,
    positionStatus: str | None = None,
    customCellPhone1: str | None = None,
    loginName: str | None = None,
) -> str:
    """通讯录查询

    根据员工姓名、手机号、邮箱等条件，在企业通讯录中查询员工信息。

    当你需要查找同事的联系方式、所属机构、职务等信息时，优先使用本工具。
    可根据实际情况传入一个或多个搜索条件，工具会自动组合条件进行查询。

    Args:
        userName: 员工姓名
        userIds: 员工ID
        orgId: 机构ID，配合 orgIds 是否级联精确匹配
        orgIds: 机构ID列表，配合 orgId 是否级联精确匹配
        orgName: 机构名称，分词匹配，不支持左右模糊
        cellPhone: 手机号，左右模糊匹配
        telNo: 座机，左右模糊匹配
        telNoExt: 座机分机，左右模糊匹配
        shortNo: 短号，左右模糊匹配
        email: 邮箱，左右模糊匹配
        ehrPosition: 职位，分词匹配，不支持左右模糊
        positionStatus: 是否兼职，精确匹配
        customCellPhone1: 用户手机号1，左右模糊匹配
        loginName: 登录名，精确匹配
    """
    try:
        return await search_contact_backend(
            userName=userName,
            userIds=userIds,
            orgId=orgId,
            orgIds=orgIds,
            orgName=orgName,
            cellPhone=cellPhone,
            telNo=telNo,
            telNoExt=telNoExt,
            shortNo=shortNo,
            email=email,
            ehrPosition=ehrPosition,
            positionStatus=positionStatus,
            customCellPhone1=customCellPhone1,
            loginName=loginName,
        )
    except httpx.TimeoutException:
        logger.error("通讯录查询请求超时。", exc_info=True)
        return json.dumps([{"error": "通讯录查询请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("通讯录查询请求失败: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"通讯录查询请求失败: {exc}"}],
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps([{"error": f"{exc}"}], ensure_ascii=False)
    except Exception as exc:
        logger.error("通讯录查询未知错误: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"通讯录查询失败: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None:
    contact_search_tool = FunctionTool(
        _contact_search_tool_impl,
        # 工具名（按用户要求中文化，对齐"个人知识库搜索"命名）
        name="contact_search",
        is_read_only=True,
    )
else:
    contact_search_tool = _contact_search_tool_impl


__all__ = [
    "CONTACT_SEARCH_FIELDS",
    "contact_search_tool",
    "search_contact_backend",
]
