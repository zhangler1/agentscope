# -*- coding: utf-8 -*-
"""read_tool_result —— 读回被持久化的完整工具输出。

模型在收到 ``<persisted-output>`` 预览后,调用本工具按 ``tool_call_id``
取回完整内容。工具为 ``ToolBase`` 子类并声明 ``is_state_injected=True``
(注意:不能走 ``FunctionTool`` —— 其 ``_extract_input_schema`` 会用 pydantic
从函数签名建 schema,``_agent_state`` 下划线开头的参数会被 pydantic 拒绝;
而框架注入状态正是以 ``_agent_state`` 为参数名。因此仿照框架内建
``_GenerateStructuredOutput``,用自定义 ``ToolBase`` + ``input_schema`` 属性
绕过签名推导)。键由当前会话构造 —— 结构性归属校验,模型无法读取其他
会话的内容。支持 ``offset`` / ``limit`` 分页。
"""
from __future__ import annotations

from typing import Any, override

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChunk

from ..tool_result_store import get_tool_result, get_tool_result_config


class ReadToolResultTool(ToolBase):  # pylint: disable=abstract-method
    """只读读回被持久化的工具输出;需要会话状态注入。"""

    name: str = "read_tool_result"
    description: str = (
        "读取被持久化保存的超长工具输出完整内容。当工具结果被替换为 "
        "<persisted-output> 预览时,通过此工具按 tool_call_id 取回完整内容,"
        "支持 offset / limit 分页。"
    )
    is_state_injected: bool = True
    is_concurrency_safe: bool = True
    is_read_only: bool = True

    # 直接覆盖 ToolBase.input_schema 注解类型,避免 property 覆写类属性
    # 触发 reportIncompatibleVariableOverride。
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "tool_call_id": {
                "type": "string",
                "description": "产出该结果的工具调用 ID(预览消息中给出)。",
            },
            "offset": {
                "type": "integer",
                "description": "起始字符偏移,默认 0。",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "最多返回字符数,默认 100000;超过单次输出上限会报错并提示分页。",
                "default": 100000,
            },
        },
        "required": ["tool_call_id"],
    }

    @override
    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """只读工具,直接放行。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} 为只读工具,允许直接调用。",
        )

    @override
    async def call(
        self,
        _agent_state: AgentState,
        **kwargs: Any,
    ) -> ToolChunk:
        """读取指定工具调用的完整输出,按 offset/limit 分页。"""
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        offset = int(kwargs.get("offset") or 0)
        limit_raw = kwargs.get("limit")

        if not tool_call_id:
            return ToolChunk(
                content=[
                    TextBlock(
                        text="错误: 缺少 tool_call_id 参数,无法定位工具输出。",
                    ),
                ],
                state=ToolResultState.ERROR,
            )

        content = await get_tool_result(
            _agent_state.session_id,
            tool_call_id,
        )
        cfg = await get_tool_result_config()
        if content is None:
            hours = cfg.ttl_seconds // 3600
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"该工具结果已过期或不存在(存储超时 {hours} 小时)。"
                            "如需重新获取,请重新运行产生该结果的命令。"
                        ),
                    ),
                ],
                state=ToolResultState.SUCCESS,
            )

        # 单次输出上限与持久化阈值动态对齐:读回输出在数学上永不超阈值,
        # 永不二次触发持久化(防读回循环,对齐 CC FileRead 的静态对齐语义)。
        max_output_chars = min(
            cfg.read_result_max_output_chars,
            cfg.per_tool_threshold_chars,
        )
        # 未显式传 limit 时默认取整页(动态上限),保证默认调用不抛错
        limit = int(limit_raw) if limit_raw not in (None, "") else max_output_chars
        if limit <= 0 or limit > max_output_chars:
            # 超限抛错(不切片返回),中文强制引导 offset/limit 分页
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"读取范围超过单次输出上限({max_output_chars} 字符)。"
                            "请使用 offset 分页读取: "
                            f"read_tool_result(tool_call_id=\"{tool_call_id}\", "
                            f"offset=<已读末尾位置>, limit=<≤{max_output_chars}>)"
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
            )
        if offset < 0:
            # 负数 offset 在 Python 切片中是"从尾部数",语义异常,直接归零
            offset = 0

        segment = content[offset:offset + limit]
        next_offset = offset + len(segment)
        if next_offset < len(content):
            remaining = len(content) - next_offset
            segment += (
                f"\n[内容还有 {remaining} 字符未读取,如需继续请调用 "
                f"read_tool_result(tool_call_id=\"{tool_call_id}\", "
                f"offset={next_offset})]"
            )
        return ToolChunk(
            content=[TextBlock(text=segment)],
            state=ToolResultState.SUCCESS,
        )


read_tool_result_tool = ReadToolResultTool()
