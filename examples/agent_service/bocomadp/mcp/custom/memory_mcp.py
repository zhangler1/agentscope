# -*- coding: utf-8 -*-
"""记忆平台 MCP 客户端（动态请求头）。

按 ``McpRegistry.load_custom()`` 的约定导出模块级 ``MCPClient`` 实例，
框架把它注入每个 workspace 的 ``default_mcps``，工具以
``mcp__euvd-memory__EUVDsearchMemory`` / ``mcp__euvd-memory__EUVDsaveMemoriesStandard``
暴露。

与 ``mobile_bank.py`` 的差别：平台的 ``guwp-token`` / ``agentId`` /
``agentPlat`` / ``runId`` 每个请求都不同，不能写死在 ``HttpMCPConfig.headers``。
这里用子类覆盖 ``_create_http_client``：stateless 模式每次 list/call 都会
新建 HTTP 客户端，此刻从 ContextVar 读当前请求的用户/智能体/运行信息，
合并进 headers——**发现工具与调用工具两个阶段都带动态头**。

不改框架源码；代价是依赖 ``MCPClient._create_http_client`` 这个私有方法，
框架升级若调整签名需要同步维护。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from agentscope.mcp import HttpMCPConfig, MCPClient

from bocomadp.config import get_app_config
from bocomadp.mcp import MEMORY_MCP_NAME

logger = logging.getLogger("as")


def _dynamic_headers(static: dict[str, str] | None) -> dict[str, str]:
    """静态头 + 当前请求上下文（空值丢弃，避免平台校验空 header）。"""
    from bocomadp.logging.trace_context import get_current_run_id
    from bocomadp.tools.agent_factory_tools import (
        _current_agent_id,
        _current_token,
    )

    headers = dict(static or {})
    headers.update(
        {
            "agentId": (_current_agent_id.get() or "").strip(),
            "agentPlat": "0",
            "runId": (get_current_run_id() or "").strip(),
            "guwp-token": (_current_token.get() or "").strip(),
        },
    )
    return {key: value for key, value in headers.items() if value}


class MemoryMCPClient(MCPClient):
    """MCP 客户端：每次建连时从 ContextVar 生成动态请求头。"""

    def _create_http_client(self) -> Any:
        """覆盖父类：合并动态头后再建 HTTP 传输。"""
        config = self.mcp_config
        if not isinstance(config, HttpMCPConfig):
            return super()._create_http_client()

        headers = _dynamic_headers(config.headers)
        if config.url.endswith("/sse") or config.url.endswith("/messages/"):
            from mcp.client.sse import sse_client

            return sse_client(
                url=config.url,
                headers=headers,
                timeout=config.timeout,
            )

        from mcp.client.streamable_http import streamable_http_client

        http_client = httpx.AsyncClient(
            headers=headers,
            timeout=config.timeout,
        )
        return streamable_http_client(
            url=config.url,
            http_client=http_client,
        )


def _build_clients() -> list[MCPClient]:
    """按 config 构建实例（检索与提取共用同一个 MCP 地址）。"""
    memory_cfg = get_app_config().memory
    if not memory_cfg.mcp_agent_key:
        logger.warning(
            "memory mcp: mcp_agent_key 未配置，跳过记忆 MCP 注册",
        )
        return []

    if memory_cfg.mcp_search_tool == memory_cfg.mcp_save_tool:
        logger.warning(
            "memory mcp: mcp_search_tool 与 mcp_save_tool 相同（%r），"
            "提取工具的参数处理不会生效，请检查 memory 配置",
            memory_cfg.mcp_search_tool,
        )

    url = memory_cfg.mcp_url
    if not url:
        return []

    try:
        return [
            MemoryMCPClient(
                name=MEMORY_MCP_NAME,
                mcp_config=HttpMCPConfig(
                    url=url,
                    headers={
                        "agent-key": memory_cfg.mcp_agent_key,
                        "mcp-protocol-version": (
                            memory_cfg.mcp_protocol_version
                        ),
                    },
                    timeout=memory_cfg.mcp_timeout_seconds,
                ),
                is_stateful=False,
                execution_timeout=memory_cfg.mcp_timeout_seconds,
            ),
        ]
    except Exception:  # noqa: BLE001 —— 配置异常不阻断启动
        logger.exception("memory mcp: failed to build client")
        return []


#: ``McpRegistry.load_custom`` 扫描到的模块级实例（未配置时为空）。
_memory_mcp_clients: list[MCPClient] = _build_clients()

# 注册表按模块命名空间里的 MCPClient 实例扫描，因此需要绑定为模块属性。
if _memory_mcp_clients:
    euvd_memory = _memory_mcp_clients[0]


__all__ = [
    "MEMORY_MCP_NAME",
    "MemoryMCPClient",
    "_dynamic_headers",
]
