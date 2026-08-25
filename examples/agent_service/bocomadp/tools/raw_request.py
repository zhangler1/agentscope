# -*- coding: utf-8 -*-
"""外数查工具（从 deerflow ``community/raw_request`` 迁移）。

【迁移改动】
- 去掉 langchain ``@tool`` 装饰器：``FunctionTool`` 会根据函数签名 /
  docstring 自动生成工具名、描述与参数 schema（同 ``cross_search``）；
- ``requests`` → ``httpx``（项目已有依赖），函数改为 async，避免阻塞
  run 任务的事件循环（同 ``cross_search``）；
- 认证 / custom_params 直接使用 bocomadp 已迁移的上下文模块
  （``deerflow/auth_context.py``、``deerflow/custom_params.py``）；
- 联机地址域名外置到 ``config.yaml`` 的 ``raw_request.api_url``
  （支持 ``$RAW_REQUEST_API_URL`` 环境变量），生产环境切换域名无需改代码。

【接口说明】
``DEFAULT_API_PATHS`` 为接口标识到联机路径的映射（原工程原样保留）；
域名（根地址）由 ``config/raw_request_config.py`` 提供。
"""
from __future__ import annotations

import json
import logging

import httpx

try:
    from agentscope.tool import FunctionTool, ToolMiddlewareBase
except ImportError:
    FunctionTool = ToolMiddlewareBase = None

from ..config.raw_request_config import get_raw_request_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers
from ..deerflow.custom_params import get_custom_params
from ._naming import tool_name

logger = logging.getLogger(__name__)

# 接口路径表（与原工程一致）：intent → 联机路径。
# 域名（根地址）外置到 config.yaml 的 raw_request.api_url，
# 生产环境通过 $RAW_REQUEST_API_URL 切换，无需改代码。
# enterprise_detail : 工商企业详情数据查询
# shell_company     : 红盾空壳公司标识查询联机接口请求
# litigation        : 最高法公开涉诉信息查询
# hd_enterprise     : 红盾企业名单查询
# intel_info        : 企业知识产权信息查询
# qycxjf_info       : 企业创新积分
# yq_info           : 舆情信息查询
# yq_info_detail    : 舆情信息详情查询
# zg_credit_info    : 中国信用目录信息
DEFAULT_API_PATHS: dict[str, str] = {
    "enterprise_detail": "/ELLM.ELLM-OFFICE.V-1.0/gsEntDetailTool.do",
    "shell_company": "/ELLM.ELLM-OFFICE.V-1.0/eicsHDShellCompanyDataTool.do",
    "litigation": "/ELLM.ELLM-OFFICE.V-1.0/eicsHighLawOutCaseInfoTool.do",
    "hd_enterprise": "/ELLM.ELLM-OFFICE.V-1.0/eicsQueryHDEnterpriseListFree.do",
    "intel_info": "/ELLM.ELLM-OFFICE.V-1.0/eicsQueryEntIntePropService.do",
    "qycxjf_info": "/ELLM.ELLM-OFFICE.V-1.0/eicsQueryQycxjfzsjPatent.do",
    "yq_info": "/ELLM.ELLM-OFFICE.V-1.0/eicsGetZsyqInfoList.do",
    "yq_info_detail": "/ELLM.ELLM-OFFICE.V-1.0/eicsGetZsyqInfoDetail.do",
    "zg_credit_info": "/ELLM.ELLM-OFFICE.V-1.0/eicsSearchXyzgCreditInfo.do",
}

# 舆情查询需要额外携带的情感参数（原逻辑）
_EMOTION_CODES = "0,1,2"


async def _raw_request_tool_impl(request_body: str, intent: str) -> str:
    """外数查工具：根据请求报文和接口标识，发送POST请求并获取返回结果。

    用户点名"外数查/外数查询/查工商/查企业详情"等即可命中本工具；根据
    intent 自动匹配联机地址并发送 POST 请求；认证头（guwp-token /
    jrt-auth-code / okic-token）及外部数据查询参数（systemCode /
    businessType / requestCause）由工具层自动注入，无需在报文中重复填写。

    Args:
        request_body: 请求报文JSON字符串（含 REQ_HEAD / REQ_BODY）。
        intent: 接口标识，支持 enterprise_detail / shell_company /
            litigation / hd_enterprise / intel_info / qycxjf_info /
            yq_info / yq_info_detail / zg_credit_info。

    Returns:
        接口返回的 JSON 字符串（失败时返回含 error 的 JSON 列表字符串）。
    """
    path = DEFAULT_API_PATHS.get(intent)
    if not path:
        valid_intents = ", ".join(DEFAULT_API_PATHS.keys())
        return json.dumps(
            [{"error": f"无效的接口标识: {intent}，支持的标识: {valid_intents}"}],
            ensure_ascii=False,
        )

    cfg = get_raw_request_config()
    url = f"{cfg.api_url.rstrip('/')}{path}"

    try:
        body = json.loads(request_body)
    except json.JSONDecodeError as exc:
        return json.dumps(
            [{"error": f"请求报文格式错误，无效的JSON: {exc}"}],
            ensure_ascii=False,
        )

    # 注入外部数据查询公共参数（来自 custom_params.tools_param.externalDataQuery）
    custom_params = get_custom_params()
    tools_param = custom_params.get("tools_param") or {}
    external = tools_param.get("externalDataQuery") or {}
    body.setdefault("REQ_BODY", {}).setdefault("param", {}).update(
        {
            "sysCode": external.get("systemCode"),
            "businessType": external.get("businessType"),
            "requestCause": external.get("requestCause"),
        },
    )

    # 舆情信息查询额外添加情感参数
    if intent == "yq_info":
        body["REQ_BODY"]["param"].update(
            {
                "newsEmotionCode": _EMOTION_CODES,
                "entEmotionCode": _EMOTION_CODES,
            },
        )

    # 注入行内认证头 + muwp 用户标识（复用三搜索工具共享的底座，避免重复实现）
    headers = build_auth_headers({})
    body = attach_muwp_user(body)

    try:
        async with httpx.AsyncClient(timeout=cfg.timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("外数查请求超时: %s", url, exc_info=True)
        return json.dumps([{"error": "请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("外数查请求失败: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"请求失败: {exc}"}],
            ensure_ascii=False,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        return json.dumps(
            [{"error": f"请求返回了无效的JSON: {exc}"}],
            ensure_ascii=False,
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


if FunctionTool is not None and ToolMiddlewareBase is not None:
    raw_request_tool = FunctionTool(
        _raw_request_tool_impl,
        # 工具名显式中文化（对齐 deerflow 原设计：行内模型点名靠中文名）。
        # 注意：行外 DeepSeek 等 API 强校验工具名 ^[a-zA-Z0-9_-]+$，
        # 中文名会被 400 拒收；行内网关不校验，可正常使用。行外环境
        # 设置 BOCOMADP_TOOL_ASCII_NAMES=1 切换英文名 raw_request_tool。
        name=tool_name("外数查", "raw_request_tool"),
        is_read_only=True,
    )
else:
    raw_request_tool = _raw_request_tool_impl
