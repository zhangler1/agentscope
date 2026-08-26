# -*- coding: utf-8 -*-
"""利率查询工具（interest_rate）。

对齐源项目 ``deerflow.community.interest_rate_search.tools``：
application/json POST，``REQ_HEAD`` + ``REQ_BODY.param{...}``，
muwp-user 模式下附加 ``REQ_BODY.muwpUser``；响应原样返回。
支持存款利率（人民币+外币，``queryDepositRates.do``）与人民币贷款
利率（``queryCNYLoanRates.do``）。

改造点（相对源项目 langchain 实现）：
- ``requests``（同步）→ ``httpx.AsyncClient``（异步，对齐本项目企业工具）；
- ``@tool("利率查询", parse_docstring=True)`` → ``FunctionTool``
  显式包装（``is_read_only=True``，查询类工具只读）；
- 认证逻辑收敛到 :func:`build_auth_headers` / :func:`attach_muwp_user`；
- 接口根地址外置到 ``config.yaml`` 的 ``rate_currency.api_url``
  （原工程 ``DEPOSIT_RATE_API_URL`` / ``LOAN_RATE_API_URL`` 环境变量），
  路径固定。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

import httpx

try:
    from agentscope.tool import FunctionTool, ToolMiddlewareBase
except ImportError:
    FunctionTool = ToolMiddlewareBase = None

from ..config.rate_currency_config import get_rate_currency_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers
from ._naming import tool_name

logger = logging.getLogger(__name__)

_DATE_PATTERN = re.compile(r"^\d{4}(\d{2}(\d{2})?)?$")


def _validate_date(date_str: str, field_name: str) -> str | None:
    if not _DATE_PATTERN.match(date_str):
        return f"{field_name}格式不正确: '{date_str}'，应为 yyyy 或 yyyyMM 或 yyyyMMdd"
    return None


#: 存款利率代码表（代码 → 名称）
DEPOSIT_RATE_CODES = {
    "15601000": "活期存款",
    "15602500": "外币单位七天通知",
    "15601100": "整存整取一月",
    "15601101": "定期存款3个月",
    "15601102": "定期存款6个月",
    "15601103": "定期存款1年",
    "15601104": "定期存款2年",
    "90001105": "定期存款3年",
    "90001106": "定期存款5年",
}

#: 贷款利率代码表（代码 → 名称）
LOAN_RATE_CODES = {
    "15605753": "个人住房公积金贷款3-5年",
    "15605754": "个人住房公积金贷款5年以上",
    "15605202": "中长期贷款3至5年不浮动",
    "15605201": "中长期贷款1至3年不浮动",
    "15605203": "中长期贷款5年以上不浮动",
    "15605000": "短期贷款6个月以下不浮动",
    "15605001": "短期贷款6个月至1年不浮动",
    "15606115": "个人住房按揭贷款5年以上",
    "15606114": "个人住房按揭贷款3-5年",
    "15603815": "贷款市场报价利率LPR五年以上",
    "15603812": "贷款市场报价利率LPR一年",
    "15605815": "交通银行LPR五年以上",
    "15605812": "交行LPR六个月至一年含一年",
}

RATE_CODES = {
    "deposit": DEPOSIT_RATE_CODES,
    "loan": LOAN_RATE_CODES,
}

#: 利率查询联机路径（域名外置到 config.yaml 的 rate_currency.api_url）
API_PATHS = {
    "deposit": "/EUVD.EUVD-JXCHAT.V-1.0/queryDepositRates.do",
    "loan": "/EUVD.EUVD-JXCHAT.V-1.0/queryCNYLoanRates.do",
}


async def _send_interest_rate_request(url: str, body: dict) -> str:
    """发送利率查询 POST 请求并返回 JSON 字符串（失败时返回含 error 的 JSON）。"""
    headers = build_auth_headers({})
    body = attach_muwp_user(body)
    try:
        async with httpx.AsyncClient(
            timeout=get_rate_currency_config().timeout,
        ) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("利率查询请求超时: %s", url, exc_info=True)
        return json.dumps([{"error": "利率查询请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("利率查询请求失败: %s", exc, exc_info=True)
        return json.dumps([{"error": f"利率查询请求失败: {exc}"}], ensure_ascii=False)

    try:
        payload = response.json()
    except ValueError as exc:
        return json.dumps(
            [{"error": f"利率查询返回了无效的JSON: {exc}"}],
            ensure_ascii=False,
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def interest_rate_backend(
    rate_type: str,
    currencyList: list[str] | None = None,
    rateCodeList: list[str] | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
) -> str:
    """执行利率查询后端请求（application/json POST）。"""
    cfg = get_rate_currency_config()
    path = API_PATHS.get(rate_type)
    if not path:
        return json.dumps(
            [{"error": f"未知的利率类型: '{rate_type}'，"
                       "请使用 'deposit'(存款) 或 'loan'(贷款)"}],
            ensure_ascii=False,
        )

    if not cfg.api_url:
        return json.dumps(
            [{"error": "利率查询API URL未配置，请在 config.yaml 的 "
                       "rate_currency.api_url 中设置。"}],
            ensure_ascii=False,
        )

    today = datetime.now()
    week_ago = today - timedelta(days=7)
    if not startDate and not endDate:
        startDate = week_ago.strftime("%Y%m%d")
        endDate = today.strftime("%Y%m%d")
    elif startDate and not endDate:
        endDate = today.strftime("%Y%m%d")
    elif endDate and not startDate:
        startDate = week_ago.strftime("%Y%m%d")

    if startDate:
        err = _validate_date(startDate, "startDate")
        if err:
            return json.dumps([{"error": err}], ensure_ascii=False)
    if endDate:
        err = _validate_date(endDate, "endDate")
        if err:
            return json.dumps([{"error": err}], ensure_ascii=False)

    if rateCodeList is not None:
        valid_codes = RATE_CODES.get(rate_type, {})
        invalid = [c for c in rateCodeList if c not in valid_codes]
        if invalid:
            valid_list = "、".join(f"{k}({v})" for k, v in valid_codes.items())
            return json.dumps(
                [{"error": f"rateCodeList中包含不合法的利率代码: {invalid}。"
                           f"rate_type='{rate_type}'时支持的利率代码: {valid_list}"}],
                ensure_ascii=False,
            )

    param: dict = {}
    if rate_type == "deposit":
        param["fvCcys"] = currencyList if currencyList is not None else []
    param["fvIntRateCodes"] = rateCodeList if rateCodeList is not None else []
    if startDate:
        param["startTime"] = startDate
    if endDate:
        param["endTime"] = endDate

    url = f"{cfg.api_url.rstrip('/')}{path}"
    body = {
        "REQ_HEAD": {},
        "REQ_BODY": {"param": param},
    }
    return await _send_interest_rate_request(url, body)


async def _interest_rate_tool_impl(
    rate_type: str,
    currencyList: list[str] | None = None,
    rateCodeList: list[str] | None = None,
    startDate: str | None = None,
    endDate: str | None = None,
) -> str:
    """查询存款或贷款利率信息。

    本工具支持查询人民币外币存款利率和人民币贷款利率。

    Args:
        rate_type: 必填。利率类型。"deposit" 查询存款利率（支持人民币和外币），
            "loan" 查询贷款利率（仅支持人民币CNY）
        currencyList: 可选。币种列表，如 ["CNY", "USD"]。仅当 rate_type="deposit"
            时生效，rate_type="loan" 时忽略。不传则查询所有币种
            注意：用户可能使用中文别名（如"美元"、"美刀"、"美币"对应USD，等），
            请将这些中文输入转换为标准ISO货币代码后再调用本工具。
            rate_type="deposit" 时支持的币种：AUD(澳大利亚元)、BEF(比利时法郎)、
            CAD(加拿大元)、CHF(瑞士法郎)、DEM(德国马克)、DKK(丹麦克朗)、
            EUR(欧元)、FRF(法国法郎)、GBP(英镑)、HKD(港币)、INR(印度卢比)、
            JPY(日元)、KRW(韩元)、MOP(澳门元)、MYR(马来西亚林吉特)、
            NLG(荷兰盾)、NOK(挪威克朗)、NZD(新西兰元)、PHP(菲律宾比索)、
            SEK(瑞典克朗)、SGD(新加坡元)、THB(泰铢)、TWD(新台币)、
            USD(美元)、CNY(人民币)
            rate_type="loan" 时仅支持：CNY(人民币)
        rateCodeList: 可选。利率代码列表，不传则查询所有利率类型。
            rate_type="deposit" 时支持的利率代码：
            15601000(活期存款)、15602500(外币单位七天通知)、15601100(整存整取一月)、
            15601101(定期存款3个月)、15601102(定期存款6个月)、15601103(定期存款1年)、
            15601104(定期存款2年)、90001105(定期存款3年)、90001106(定期存款5年)
            rate_type="loan" 时支持的利率代码：
            15605753(个人住房公积金贷款3-5年)、15605754(个人住房公积金贷款5年以上)、
            15605202(中长期贷款3至5年不浮动)、15605201(中长期贷款1至3年不浮动)、
            15605203(中长期贷款5年以上不浮动)、15605000(短期贷款6个月以下不浮动)、
            15605001(短期贷款6个月至1年不浮动)、15606115(个人住房按揭贷款5年以上)、
            15606114(个人住房按揭贷款3-5年)、15603815(贷款市场报价利率LPR五年以上)、
            15603812(贷款市场报价利率LPR一年)、15605815(交通银行LPR五年以上)、
            15605812(交行LPR六个月至一年含一年)
        startDate: 建议填写。开始时间，格式 yyyy 或 yyyyMM 或 yyyyMMdd。
            未指定时默认为7天前
        endDate: 建议填写。结束时间，格式 yyyy 或 yyyyMM 或 yyyyMMdd。
            未指定时默认为今天

    Example:
        {"rate_type": "deposit", "currencyList": ["CNY", "USD"],
         "rateCodeList": ["15601103"], "startDate": "202401",
         "endDate": "20241231"}
    """
    try:
        return await interest_rate_backend(
            rate_type,
            currencyList,
            rateCodeList,
            startDate,
            endDate,
        )
    except Exception as exc:
        logger.error("利率查询未知错误: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"利率查询失败: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None and ToolMiddlewareBase is not None:
    interest_rate_tool = FunctionTool(
        _interest_rate_tool_impl,
        # 工具名显式中文化（对齐 deerflow 原设计：行内模型点名靠中文名）。
        # 注意：行外 DeepSeek 等 API 强校验工具名 ^[a-zA-Z0-9_-]+$，
        # 中文名会被 400 拒收；行内网关不校验，可正常使用。行外环境
        # 设置 BOCOMADP_TOOL_ASCII_NAMES=1 切换英文名 interest_rate。
        name=tool_name("利率查询", "interest_rate"),
        is_read_only=True,
    )
else:
    interest_rate_tool = _interest_rate_tool_impl


__all__ = [
    "API_PATHS",
    "DEPOSIT_RATE_CODES",
    "LOAN_RATE_CODES",
    "RATE_CODES",
    "interest_rate_tool",
    "interest_rate_backend",
]
