# -*- coding: utf-8 -*-
"""工具目录 —— "可配置工具集合 M" 的**纯常量**单一数据源。

集中定义 M 的静态来源与名称，供 :mod:`bocomadp.routers.agent_tools`
（工具配置接口）与 :mod:`bocomadp.tools.agent_factory_tools`（智能体工厂
工具）共用，避免多处硬编码导致"菜单名 / 可配名 / 运行时名"三处不一致。

M 由五个来源组成：

1. **workspace builtins** —— 运行时真值为首字母大写（``Bash`` 等）；
2. **项目工具** —— ``ToolRegistry`` 扫描 ``builtin_tools.py`` / ``custom/``；
3. **MCP 服务器名** —— ``McpRegistry``；
4. **框架团队/规划工具** —— ``get_toolkit`` 挂载的 ``Team*`` / ``Task*``；
5. **企业工具** —— 见 :mod:`bocomadp.tools.enterprise_catalog`。

.. note::
   本模块**故意不导入** ``bocomadp.tools`` 下的任何模块（哪怕函数内延迟
   导入），否则会与 ``tools/__init__.py`` 形成导入环。企业工具实例的
   解析放在 :mod:`bocomadp.tools.enterprise_catalog`。
"""

from __future__ import annotations

#: 内置智能体（智能体工厂）的 agent_id（下划线前缀表示系统内置）。
AGENT_CREATOR_ID = "_agent-creator"

# ---------------------------------------------------------------------------
# 1. workspace builtins
# ---------------------------------------------------------------------------
#: builtins 的**运行时真值**（首字母大写）。
#: 仅支持 Linux：运行时 shell 工具为 ``Bash``；Windows 下为 ``PowerShell``，
#: 本项目不覆盖。
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
)

#: builtins 的展示元数据（name + description）。
BUILTIN_TOOLS_META: tuple[dict[str, str], ...] = (
    {
        "name": "Bash",
        "description": (
            "在工作区沙箱中执行bash命令。"
            "命令在工作区目录中运行，可以读写文件、安装包和执行脚本。"
        ),
    },
    {
        "name": "Read",
        "description": "读取工作区中文件的内容。支持为大型文件选择行范围。",
    },
    {
        "name": "Write",
        "description": "向工作区中的文件写入内容。会自动创建父目录。",
    },
    {
        "name": "Edit",
        "description": (
            "在现有文件中执行精确的字符串替换。"
            "适用于无需重写整个文件的有针对性修改。"
        ),
    },
    {
        "name": "Glob",
        "description": (
            "查找匹配glob模式的文件（例如 ``**/*.py``）。返回相对文件路径。"
        ),
    },
    {
        "name": "Grep",
        "description": (
            "使用正则表达式搜索文件内容。支持基于ripgrep的完整正则语法。"
        ),
    },
)

#: 旧的小写 builtin 名 → 运行时大写名的归一化映射。
_BUILTIN_BY_LOWER: dict[str, str] = {n.lower(): n for n in BUILTIN_TOOL_NAMES}


def canonical_tool_name(name: str) -> str:
    """把 builtins 的历史小写名归一为运行时大写名；其余名字原样返回。

    Args:
        name (str): 待归一的工具名。

    Returns:
        str: 归一后的工具名。
    """
    stripped = (name or "").strip()
    return _BUILTIN_BY_LOWER.get(stripped.lower(), stripped)


# ---------------------------------------------------------------------------
# 4. 框架团队/规划工具
# ---------------------------------------------------------------------------
#: 团队/规划工具的静态元数据（name + 简短 description）。
#: 由 ``get_toolkit`` 挂载，纳入可配置集合。
FRAMEWORK_TOOLS_META: tuple[dict[str, str], ...] = (
    {
        "name": "TeamCreate",
        "description": "以当前会话为领导创建一个新团队，用于拆分子任务并行执行。",
    },
    {
        "name": "AgentCreate",
        "description": "为团队创建专业化的成员智能体，配置角色、提示词与权限。",
    },
    {
        "name": "TeamSay",
        "description": "向团队领导者或所有成员发送消息、广播与协调进度。",
    },
    {
        "name": "TeamDelete",
        "description": "解散当前团队并删除其所有成员智能体与会话（不可逆）。",
    },
    {
        "name": "AgentInvite",
        "description": "邀请其他可邀请的智能体加入当前团队。",
    },
    {
        "name": "TaskCreate",
        "description": "为当前会话创建结构化的任务列表以跟踪进度。",
    },
    {
        "name": "TaskList",
        "description": "列出任务列表中的所有任务。",
    },
    {
        "name": "TaskGet",
        "description": "按 ID 从任务列表中检索单个任务。",
    },
    {
        "name": "TaskUpdate",
        "description": "更新任务列表中的任务（状态、内容等）。",
    },
)

# ---------------------------------------------------------------------------
# 5. 企业工具的排除名单（实例解析见 enterprise_catalog）
# ---------------------------------------------------------------------------
#: 企业工具中**不纳入**可配置集合的名字（覆盖中英文两种形态）。
#: - 联网搜索：按要求暂不纳入；
#: - query_internal_doc / submit_it_ticket：占位实现，暂不纳入。
ENTERPRISE_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {
        "online_search",
        "联网搜索",
        "query_internal_doc",
        "submit_it_ticket",
    },
)


__all__ = [
    "AGENT_CREATOR_ID",
    "BUILTIN_TOOL_NAMES",
    "BUILTIN_TOOLS_META",
    "FRAMEWORK_TOOLS_META",
    "ENTERPRISE_EXCLUDED_NAMES",
    "canonical_tool_name",
]
