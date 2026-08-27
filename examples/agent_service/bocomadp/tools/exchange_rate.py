# -*- coding: utf-8 -*-
"""汇率查询工具（exchange_rate）。

对齐源项目 ``deerflow.community.exchange_rate_search.tools``：
application/json POST，``REQ_HEAD`` + ``REQ_BODY.param{baseCurrency,
targetCurrency}``，muwp-user 模式下附加 ``REQ_BODY.muwpUser``；
响应原样返回（含购汇/购钞/结汇/结钞兑换金额）。

改造点（相对源项目 langchain 实现）：
- ``requests``（同步）→ ``httpx.AsyncClient``（异步，对齐本项目企业工具）；
- ``@tool`` → ``FunctionTool`` 显式包装（``is_read_only=True``，查询类只读）；
- 认证逻辑收敛到 :func:`build_auth_headers` / :func:`attach_muwp_user`；
- 接口根地址外置到 ``config.yaml`` 的 ``rate_currency.api_url``
  （原工程 ``EXCHANGE_RATE_API_URL`` 环境变量），路径固定。
"""
from __future__ import annotations

import json
import logging

import httpx

try:
    from agentscope.tool import FunctionTool, ToolMiddlewareBase
except ImportError:
    FunctionTool = ToolMiddlewareBase = None

from ..config.rate_currency_config import get_rate_currency_config
from ..deerflow.auth_context import attach_muwp_user, build_auth_headers
from ._naming import tool_name

logger = logging.getLogger(__name__)

#: 汇率查询联机路径（域名外置到 config.yaml 的 rate_currency.api_url）
EXCHANGE_RATE_PATH = "/EUVD.EUVD-JXCHAT.V-1.0/queryExchangeRates.do"


async def _send_exchange_rate_request(url: str, body: dict) -> str:
    """发送汇率查询 POST 请求并返回 JSON 字符串（失败时返回含 error 的 JSON）。"""
    headers = build_auth_headers({})
    body = attach_muwp_user(body)
    try:
        async with httpx.AsyncClient(
            timeout=get_rate_currency_config().timeout,
        ) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.error("汇率查询请求超时: %s", url, exc_info=True)
        return json.dumps([{"error": "汇率查询请求超时。"}], ensure_ascii=False)
    except httpx.HTTPError as exc:
        logger.error("汇率查询请求失败: %s", exc, exc_info=True)
        return json.dumps([{"error": f"汇率查询请求失败: {exc}"}], ensure_ascii=False)

    try:
        payload = response.json()
    except ValueError as exc:
        return json.dumps(
            [{"error": f"汇率查询返回了无效的JSON: {exc}"}],
            ensure_ascii=False,
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def exchange_rate_backend(
    baseCurrency: list[str] | None = None,
    targetCurrency: list[str] | None = None,
) -> str:
    """执行汇率查询后端请求（application/json POST）。"""
    cfg = get_rate_currency_config()
    if not cfg.api_url:
        return json.dumps(
            [{"error": "汇率查询API URL未配置，请在 config.yaml 的 "
                       "rate_currency.api_url 中设置。"}],
            ensure_ascii=False,
        )
    if not baseCurrency or not targetCurrency:
        return json.dumps(
            [{"error": "baseCurrency 和 targetCurrency 为必填参数，"
                       "请传入具体的币种列表。"}],
            ensure_ascii=False,
        )

    url = f"{cfg.api_url.rstrip('/')}{EXCHANGE_RATE_PATH}"
    param: dict = {
        "baseCurrency": baseCurrency,
        "targetCurrency": targetCurrency,
    }
    body = {
        "REQ_HEAD": {},
        "REQ_BODY": {"param": param},
    }
    return await _send_exchange_rate_request(url, body)


async def _exchange_rate_tool_impl(
    baseCurrency: list[str] | None = None,
    targetCurrency: list[str] | None = None,
) -> str:
    """查询指定币种的汇率信息。

    本工具查询指定币种的汇率信息，包括购汇、购钞、结汇、结钞的兑换金额。

    Args:
        baseCurrency: 必填。基准货币列表，如 ["USD", "EUR"]。
            注意：用户可能使用中文别名（如"美元"、"美刀"、"美币"对应USD等），
            请将这些中文输入转换为标准ISO货币代码后再调用本工具。
            支持的币种：EUR(欧元)、RUB(俄罗斯卢布)、AED(阿联酋迪拉姆)、VND(越南盾)、
            KZT(哈萨克斯坦坚戈)、MOP(澳门元)、JPY(日元)、MXN(墨西哥比索)、HKD(港币)、
            SGD(新加坡元)、USD(美元)、TWD(新台币)、PLN(波兰兹罗提)、CHF(瑞士法郎)、
            DKK(丹麦克朗)、INR(印度卢比)、CAD(加拿大元)、SEK(瑞典克朗)、THB(泰铢)、
            IDR(印尼卢比)、AUD(澳大利亚元)、NOK(挪威克朗)、SAR(沙特里亚尔)、
            PHP(菲律宾比索)、BRL(巴西雷亚尔)、MYR(马来西亚林吉特)、ZAR(南非兰特)、
            KRW(韩元)、GBP(英镑)、NZD(新西兰元)、HUF(匈牙利福林)
        targetCurrency: 必填。目标货币列表，如 ["CNY"]。注意事项同上。

    Example:
        {"baseCurrency": ["USD", "EUR"], "targetCurrency": ["CNY"]}
    """
    try:
        return await exchange_rate_backend(baseCurrency, targetCurrency)
    except Exception as exc:
        logger.error("汇率查询未知错误: %s", exc, exc_info=True)
        return json.dumps(
            [{"error": f"汇率查询失败: {exc}"}],
            ensure_ascii=False,
        )


if FunctionTool is not None and ToolMiddlewareBase is not None:
    exchange_rate_tool = FunctionTool(
        _exchange_rate_tool_impl,
        # 工具名显式中文化（对齐 deerflow 原设计：行内模型点名靠中文名）。
        # 注意：行外 DeepSeek 等 API 强校验工具名 ^[a-zA-Z0-9_-]+$，
        # 中文名会被 400 拒收；行内网关不校验，可正常使用。行外环境
        # 设置 BOCOMADP_TOOL_ASCII_NAMES=1 切换英文名 exchange_rate。
        name=tool_name("汇率查询", "exchange_rate"),
        is_read_only=True,
    )
else:
    exchange_rate_tool = _exchange_rate_tool_impl


__all__ = [
    "EXCHANGE_RATE_PATH",
    "exchange_rate_tool",
    "exchange_rate_backend",
]
