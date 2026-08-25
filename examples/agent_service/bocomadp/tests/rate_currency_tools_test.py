# -*- coding: utf-8 -*-
"""利率汇率工具单元测试（bocomadp.tools.exchange_rate / interest_rate）。

离线测试：mock ``httpx.AsyncClient``，不依赖行内网关 12.244.167.46。

运行：
    cd d:/AIproject/agentscope
    python examples/agent_service/bocomadp/tests/rate_currency_tools_test.py -v
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from unittest.async_case import IsolatedAsyncioTestCase

import httpx

# ---------------------------------------------------------------------------
# 手动加载 bocomadp 模块树（绕过包级 __init__.py 的重依赖：
# config 包会 import pydantic 全家、tools 包会 import 全部企业工具，
# 本地单测只需 3 个模块：rate_currency_config / auth_context / 两个工具）。
# 本文件位于 bocomadp/tests/ 下，bocomadp 包根 = 上两级目录。
# ---------------------------------------------------------------------------
_BOCOMADP_DIR = Path(__file__).resolve().parent.parent


def _make_package(name: str) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = []  # 占位：父包存在即可解析相对导入
    sys.modules[name] = pkg
    return pkg


def _load_module(name: str, rel_path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, _BOCOMADP_DIR / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_make_package("bocomadp")
_make_package("bocomadp.config")
_make_package("bocomadp.deerflow")
_make_package("bocomadp.tools")

_load_module("bocomadp.config.base", "config/base.py")
_load_module("bocomadp.config.rate_currency_config", "config/rate_currency_config.py")
_load_module("bocomadp.deerflow._session_store", "deerflow/_session_store.py")
_load_module("bocomadp.deerflow.auth_context", "deerflow/auth_context.py")
_load_module("bocomadp.tools.exchange_rate", "tools/exchange_rate.py")
_load_module("bocomadp.tools.interest_rate", "tools/interest_rate.py")

from bocomadp.config import rate_currency_config as rcc  # noqa: E402
from bocomadp.config.rate_currency_config import (  # noqa: E402
    DEFAULT_API_URL,
    RateCurrencyConfig,
)
from bocomadp.deerflow.auth_context import (  # noqa: E402
    ResolvedAuth,
    reset_resolved_auth,
    set_resolved_auth,
)
from bocomadp.tools import exchange_rate, interest_rate  # noqa: E402


# ---------------------------------------------------------------- fakes
class _FakeResponse:
    """模拟 httpx.Response：可选成功 / 抛错 / 无效 JSON。"""

    def __init__(self, payload=None, error=None, invalid_json=False):
        self._payload = payload
        self._error = error
        self._invalid_json = invalid_json

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self):
        if self._invalid_json:
            raise ValueError("Expecting value: line 1 column 1")
        return self._payload


class _FakeAsyncClient:
    """模拟 httpx.AsyncClient：记录最后一次 (url, headers, body)。"""

    last_call: tuple = None

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).last_call = (url, headers, json)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeDateTime(datetime):
    """固定时钟：2026-08-25 12:00，用于日期默认值断言。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 25, 12, 0, 0)


# ---------------------------------------------------------------- 汇率
class ExchangeRateToolTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeAsyncClient.last_call = None  # 每次测试重置共享记录
        self.cfg = RateCurrencyConfig(api_url="http://12.244.167.46", timeout=30.0)
        p_cfg = mock.patch.object(
            exchange_rate, "get_rate_currency_config", return_value=self.cfg
        )
        p_cfg.start()
        self.addCleanup(p_cfg.stop)

    def _patch_client(self, response=None, error=None) -> _FakeAsyncClient:
        fake = _FakeAsyncClient(response=response, error=error)
        p = mock.patch("httpx.AsyncClient", return_value=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    async def test_missing_params(self) -> None:
        """必填参数缺失 → 返回错误，不发请求。"""
        result = await exchange_rate.exchange_rate_backend(None, ["CNY"])
        data = json.loads(result)
        self.assertIn("baseCurrency 和 targetCurrency 为必填参数", data[0]["error"])
        self.assertIsNone(_FakeAsyncClient.last_call)

    async def test_api_url_not_configured(self) -> None:
        """api_url 为空 → 返回未配置错误。"""
        self.cfg.api_url = ""
        result = await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        data = json.loads(result)
        self.assertIn("API URL未配置", data[0]["error"])

    async def test_success_request(self) -> None:
        """正常请求：URL 拼装、报文结构、响应原样返回。"""
        payload = {"code": "0", "data": [{"currency": "USD/CNY", "amount": "7.20"}]}
        self._patch_client(response=_FakeResponse(payload=payload))
        result = await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        self.assertEqual(json.loads(result), payload)

        url, headers, body = _FakeAsyncClient.last_call
        self.assertTrue(url.startswith("http://12.244.167.46"))
        self.assertTrue(url.endswith("/queryExchangeRates.do"))
        self.assertEqual(
            body,
            {
                "REQ_HEAD": {},
                "REQ_BODY": {
                    "param": {"baseCurrency": ["USD"], "targetCurrency": ["CNY"]}
                },
            },
        )
        # 默认 auth_mode=none：不注入任何认证头
        self.assertNotIn("guwp-token", headers)

    async def test_muwp_user_attached(self) -> None:
        """muwp-user 认证模式下，REQ_BODY 附加 muwpUser。"""
        token = set_resolved_auth(
            ResolvedAuth(auth_mode="muwp-user", muwp_user={"userId": "lrm"})
        )
        self.addCleanup(reset_resolved_auth, token)
        self._patch_client(response=_FakeResponse(payload={}))
        await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        _, _, body = _FakeAsyncClient.last_call
        self.assertEqual(body["REQ_BODY"]["muwpUser"], {"userId": "lrm"})

    async def test_timeout(self) -> None:
        """超时 → 返回"请求超时"错误。"""
        self._patch_client(error=httpx.TimeoutException("timed out"))
        result = await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        self.assertIn("请求超时", json.loads(result)[0]["error"])

    async def test_http_error(self) -> None:
        """HTTP 失败 → 返回"请求失败"错误。"""
        req = httpx.Request("POST", "http://12.244.167.46/x")
        resp = httpx.Response(500, request=req)
        self._patch_client(
            error=httpx.HTTPStatusError("Server error", request=req, response=resp)
        )
        result = await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        self.assertIn("请求失败", json.loads(result)[0]["error"])

    async def test_invalid_json(self) -> None:
        """响应不是合法 JSON → 返回"无效的JSON"错误。"""
        self._patch_client(response=_FakeResponse(payload=None, invalid_json=True))
        result = await exchange_rate.exchange_rate_backend(["USD"], ["CNY"])
        self.assertIn("无效的JSON", json.loads(result)[0]["error"])


# ---------------------------------------------------------------- 利率
class InterestRateToolTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeAsyncClient.last_call = None  # 每次测试重置共享记录
        self.cfg = RateCurrencyConfig(api_url="http://12.244.167.46", timeout=30.0)
        p_cfg = mock.patch.object(
            interest_rate, "get_rate_currency_config", return_value=self.cfg
        )
        p_cfg.start()
        self.addCleanup(p_cfg.stop)
        # 固定时钟，便于断言日期默认值
        p_time = mock.patch.object(interest_rate, "datetime", _FakeDateTime)
        p_time.start()
        self.addCleanup(p_time.stop)

    def _patch_client(self, response=None, error=None) -> _FakeAsyncClient:
        fake = _FakeAsyncClient(response=response, error=error)
        p = mock.patch("httpx.AsyncClient", return_value=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    async def test_unknown_rate_type(self) -> None:
        """未知利率类型 → 错误信息含支持范围。"""
        result = await interest_rate.interest_rate_backend("bond")
        data = json.loads(result)
        self.assertIn("未知的利率类型", data[0]["error"])
        self.assertIn("deposit", data[0]["error"])

    async def test_api_url_not_configured(self) -> None:
        """api_url 为空 → 返回未配置错误。"""
        self.cfg.api_url = ""
        result = await interest_rate.interest_rate_backend("deposit")
        self.assertIn("API URL未配置", json.loads(result)[0]["error"])

    async def test_default_date_range(self) -> None:
        """不传日期 → 默认近 7 天（固定时钟 2026-08-25 → 08-18 ~ 08-25）。"""
        self._patch_client(response=_FakeResponse(payload={}))
        await interest_rate.interest_rate_backend("deposit")
        _, _, body = _FakeAsyncClient.last_call
        param = body["REQ_BODY"]["param"]
        self.assertEqual(param["startTime"], "20260818")
        self.assertEqual(param["endTime"], "20260825")

    async def test_invalid_date_format(self) -> None:
        """非法日期格式 → 报错并提示 yyyy/yyyyMM/yyyyMMdd。"""
        result = await interest_rate.interest_rate_backend(
            "deposit", startDate="2026-8-1"
        )
        self.assertIn("格式不正确", json.loads(result)[0]["error"])

    async def test_invalid_rate_code(self) -> None:
        """非法利率代码 → 报错并列出支持的代码表。"""
        result = await interest_rate.interest_rate_backend(
            "deposit", rateCodeList=["11111111"]
        )
        data = json.loads(result)
        self.assertIn("rateCodeList中包含不合法的利率代码", data[0]["error"])
        self.assertIn("15601103", data[0]["error"])

    async def test_valid_rate_code_passes(self) -> None:
        """合法利率代码 → 正常发请求，响应原样返回。"""
        payload = [{"code": "0", "data": [{"rateCode": "15601103", "rate": "1.75"}]}]
        self._patch_client(response=_FakeResponse(payload=payload))
        result = await interest_rate.interest_rate_backend(
            "deposit", rateCodeList=["15601103"]
        )
        self.assertEqual(json.loads(result), payload)
        url, _, _ = _FakeAsyncClient.last_call
        self.assertTrue(url.endswith("/queryDepositRates.do"))

    async def test_deposit_param_contains_currency(self) -> None:
        """存款：param 含 fvCcys（币种）与 fvIntRateCodes。"""
        self._patch_client(response=_FakeResponse(payload={}))
        await interest_rate.interest_rate_backend(
            "deposit", currencyList=["CNY", "USD"], rateCodeList=["15601103"]
        )
        url, _, body = _FakeAsyncClient.last_call
        self.assertTrue(url.endswith("/queryDepositRates.do"))
        param = body["REQ_BODY"]["param"]
        self.assertEqual(param["fvCcys"], ["CNY", "USD"])
        self.assertEqual(param["fvIntRateCodes"], ["15601103"])

    async def test_loan_param_ignores_currency(self) -> None:
        """贷款：param 无 fvCcys（贷款仅人民币，忽略 currencyList）。"""
        self._patch_client(response=_FakeResponse(payload={}))
        await interest_rate.interest_rate_backend(
            "loan", currencyList=["USD"], rateCodeList=["15603812"]
        )
        url, _, body = _FakeAsyncClient.last_call
        self.assertTrue(url.endswith("/queryCNYLoanRates.do"))
        param = body["REQ_BODY"]["param"]
        self.assertNotIn("fvCcys", param)
        self.assertEqual(param["fvIntRateCodes"], ["15603812"])

    async def test_timeout(self) -> None:
        """超时 → 返回"请求超时"错误。"""
        self._patch_client(error=httpx.TimeoutException("timed out"))
        result = await interest_rate.interest_rate_backend("deposit")
        self.assertIn("请求超时", json.loads(result)[0]["error"])


# ---------------------------------------------------------------- 配置
class RateCurrencyConfigTest(unittest.TestCase):
    def test_from_yaml(self) -> None:
        """正常读取 rate_currency 节点。"""
        fake_yaml = {"rate_currency": {"api_url": "http://10.0.0.1", "timeout": 15}}
        with mock.patch.object(rcc._base, "load_config_yaml", return_value=fake_yaml):
            cfg = RateCurrencyConfig.from_yaml()
        self.assertEqual(cfg.api_url, "http://10.0.0.1")
        self.assertEqual(cfg.timeout, 15.0)

    def test_missing_section_uses_defaults(self) -> None:
        """节点缺失 → 使用默认域名与 30s 超时。"""
        with mock.patch.object(rcc._base, "load_config_yaml", return_value={}):
            cfg = RateCurrencyConfig.from_yaml()
        self.assertEqual(cfg.api_url, DEFAULT_API_URL)
        self.assertEqual(cfg.timeout, 30.0)


if __name__ == "__main__":
    unittest.main()
