"""MCP 子包：MCPClient 注册表 + custom/ 自动扫描。"""

from bocomadp.mcp.registry import McpRegistry

#: 内置记忆 MCP 的客户端名（工具名前缀 ``mcp__euvd-memory__``）。
#: 定义在这里作为单一真值来源：``custom/memory_mcp.py`` 用它注册，
#: 工具包装层与 workspace 白名单也引用同一份定义。
MEMORY_MCP_NAME = "euvd-memory"

__all__ = ["McpRegistry", "MEMORY_MCP_NAME"]
