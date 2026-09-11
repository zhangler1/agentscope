# -*- coding: utf-8 -*-
"""手机银行 MCP —— 无状态 HTTP 接入。

``McpRegistry.load_custom()`` 自动扫描本模块并注册导出的 ``MCPClient``
实例，无需修改 main.py，重启生效。注册后：

- 每个新建 workspace 的默认 MCP（``default_mcps``）包含本服务；
- 智能体工厂 / 工具白名单路由经 ``app.state.mcp_registry`` 可见；
- 工具以 ``mcp__mobile-bank__<tool>`` 命名暴露给智能体。
"""
from agentscope.mcp import MCPClient, HttpMCPConfig

mobile_bank = MCPClient(
    name="mobile-bank",
    mcp_config=HttpMCPConfig(url="http://12.244.107.162:8000/mcp"),
    is_stateful=False,
)
