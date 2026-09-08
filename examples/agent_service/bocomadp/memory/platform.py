# -*- coding: utf-8 -*-
"""记忆平台 HTTP 客户端：REQ_MESSAGE 表单信封 + 三接口调用与响应判定。

传输：``x-www-form-urlencoded``，JSON 信封放 ``REQ_MESSAGE`` 字段；请求头
``Jumpcloud-Env: BASE``；Cookie 不传。平台接口（契约 docs/记忆.txt）：

- 注册 ``/registerAgent.do``：TRAN_SUCCESS=="1" 且 result.caller 非空才成功
- 检索 ``/searchMemory.do``：TRAN_SUCCESS=="1"，返回 result 记忆条目列表
- 录入 ``/saveMemoriesStandard.do``：RSP_BODY.result == "success"

接口地址完整 URL（不拼接）来自 ``config.memory.{register,retrieve,extract}_url``
（config.yaml ``memory:`` 节点，见 Task 6）；本模块通过 ``_get_url`` 选择配置键。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from bocomadp.config import get_app_config

logger = logging.getLogger("as")

# 接口名 → AppConfig.memory 中的完整 URL 配置键
_URL_CONFIG_KEYS = {
    "register": "register_url",
    "retrieve": "retrieve_url",
    "extract": "extract_url",
}


class PlatformError(RuntimeError):
    """平台接口调用失败（HTTP 错误 / TRAN_SUCCESS!=1 / result!=success / 未配置）。"""


def _get_url(name: str) -> str:
    """返回平台接口完整 URL；未配置抛 PlatformError（测试中可 monkeypatch）。"""
    memory_cfg = getattr(get_app_config(), "memory", None)
    key = _URL_CONFIG_KEYS[name]
    url = getattr(memory_cfg, key, "") if memory_cfg is not None else ""
    if not url:
        raise PlatformError(f"memory.{key} 未配置")
    return url


def _timeout_seconds() -> float:
    """平台请求超时：config.memory.request_timeout_seconds（缺省 30s）。"""
    memory_cfg = getattr(get_app_config(), "memory", None)
    timeout = (
        getattr(memory_cfg, "request_timeout_seconds", None)
        if memory_cfg is not None
        else None
    )
    return float(timeout or 30)


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Jumpcloud-Env": "BASE",
    }


async def _post_form(url: str, param: dict[str, Any]) -> dict[str, Any]:
    """POST REQ_MESSAGE 表单信封，返回响应 JSON。

    信封结构（与项目内 online_search/raw_request 一致）：
    ``{"REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""}, "REQ_BODY": {"param": param}}``

    调试：请求参数与响应**原始报文**（未解析文本）以 DEBUG 级别输出，
    便于核对平台契约与联调（正式环境日志级别 INFO 时不输出）。
    """
    envelope = {
        "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
        "REQ_BODY": {"param": param},
    }
    request_json = json.dumps(envelope, ensure_ascii=False)
    try:
        async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
            resp = await client.post(
                url,
                data={"REQ_MESSAGE": request_json},
                headers=_headers(),
            )
            # 先读原始报文再 raise：HTTP 错误响应也要能看到原始输出
            raw_text = resp.text
            logger.debug(
                "memory: platform request url=%s envelope=%s",
                url,
                request_json,
            )
            logger.debug(
                "memory: platform response url=%s status=%s raw=%s",
                url,
                resp.status_code,
                raw_text,
            )
            resp.raise_for_status()
            try:
                return json.loads(raw_text)
            except (json.JSONDecodeError, TypeError):
                raise PlatformError(
                    f"platform response not json: status={resp.status_code} "
                    f"body={raw_text[:500]!r}",
                ) from None
    except Exception as exc:  # noqa: BLE001 — 统一转为 PlatformError
        logger.debug(
            "memory: platform request failed url=%s param=%s err=%s",
            url,
            json.dumps(param, ensure_ascii=False),
            exc,
        )
        raise PlatformError(f"platform request failed: {exc}") from exc


async def register_agent(param: dict[str, Any]) -> str:
    """注册接口：TRAN_SUCCESS=="1" 且返回 caller；成功返回 caller。"""
    resp = await _post_form(_get_url("register"), param)
    if resp.get("RSP_HEAD", {}).get("TRAN_SUCCESS") != "1":
        raise PlatformError(f"register failed: {resp}")
    result = resp.get("RSP_BODY", {}).get("result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    caller = result.get("caller") or ""
    if not caller:
        raise PlatformError(f"register response missing caller: {resp}")
    return caller


async def search_memory(param: dict[str, Any]) -> list[dict[str, Any]]:
    """检索接口：TRAN_SUCCESS=="1"；返回记忆条目列表（无则空列表）。"""
    resp = await _post_form(_get_url("retrieve"), param)
    if resp.get("RSP_HEAD", {}).get("TRAN_SUCCESS") != "1":
        raise PlatformError(f"search failed: {resp}")
    result = resp.get("RSP_BODY", {}).get("result") or []
    return result if isinstance(result, list) else []


async def save_memories(param: dict[str, Any]) -> None:
    """录入接口：RSP_BODY.result == "success" 判定。"""
    resp = await _post_form(_get_url("extract"), param)
    result = resp.get("RSP_BODY", {}).get("result")
    if result != "success":
        raise PlatformError(f"save failed: {resp}")


__all__ = [
    "PlatformError",
    "register_agent",
    "search_memory",
    "save_memories",
    "_get_url",
    "_post_form",
]
