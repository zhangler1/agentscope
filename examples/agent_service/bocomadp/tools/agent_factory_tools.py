# -*- coding: utf-8 -*-
"""Agent factory tools — used by the agent-creator to manage agent configs.

These tools call the framework's built-in ``/agent`` REST API
(StorageBase → Postgres) so every agent the agent-creator produces
is persisted, user-scoped, and visible to the framework's native
endpoints.

Tool list:

- ``create_agent``          — create a new agent (id auto-generated)
- ``update_agent``          — update an existing agent
- ``delete_agent``          — delete an agent
- ``list_agents``           — list current user's agents
- ``get_agent``             — get one agent's full config
- ``get_agent_tools``       — list the tools enabled for one agent
- ``list_tools_for_agent``  — list all available tools + MCPs
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import urllib.parse
from typing import Any

import httpx

from ..tool_catalog import (
    BUILTIN_TOOL_NAMES,
    BUILTIN_TOOLS_META,
    FRAMEWORK_TOOLS_META,
    canonical_tool_name,
)
from .enterprise_catalog import enterprise_tools_meta

# 用框架 logger "as"：``apply_logging_level`` 会把 "as" 一起调到
# config.yaml 的 ``log_level``，因此 ``log_level: debug`` 时工厂工具的
# 请求/响应明细会一并输出（与 bocomadp/memory/* 等模块一致）。
logger = logging.getLogger("as")

try:
    from agentscope.tool import FunctionTool
except ImportError:
    FunctionTool = None  # type: ignore[assignment]


def tool(fn=None, **opts):
    """@tool 装饰器：把工具函数包装为框架 FunctionTool（ToolBase）。

    新版 agentscope 不再提供 ``agentscope.tool.tool`` 装饰器；直接
    用 FunctionTool 包装，保证注入 Toolkit 的对象是 ToolBase 实例
    （否则框架 remove_tool 访问 tool.name 时会对裸函数抛
    AttributeError）。agentscope 未安装时（仅静态检查场景）原样返回。
    """
    def _wrap(f):
        if FunctionTool is None:
            return f
        return FunctionTool(f, **opts)

    if fn is not None:
        return _wrap(fn)
    return _wrap


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_tool_registry: Any = None
_mcp_registry: Any = None

# Shared user-id context — set by ``build_agent_tools`` (main.py) on each
# chat run so factory tools know which user is calling.
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_factory_user_id", default="default",
)

# Shared guwp token context — set by ``TokenCaptureMiddleware`` (main.py)
# from the ``guwpToken`` request header.  The framework's ChatRunRegistry
# spawns the chat run via ``asyncio.create_task``, which copies the current
# context, so the token is visible to factory tools inside the run.
_current_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_factory_token", default="",
)

# Current agent-creator session id — set by ``build_agent_tools`` (main.py)
# on each chat run. Available to factory tools that need session context.
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_factory_session_id", default="",
)

# Internal API root (same process, localhost is safe).
# 服务以 ``root_app`` 启动时所有路由挂在 ``/api`` 下
# （main.py 末尾：``root_app.mount("/api", app)``，Dockerfile CMD 即
# ``python main.py``），因此默认带 ``/api`` 前缀。
# 若改用 ``uvicorn main:app``（内层 app、无前缀）启动，可设
# ``BOCOMADP_INTERNAL_API_BASE=http://localhost:8000`` 覆盖。
_INTERNAL_API_BASE = os.environ.get(
    "BOCOMADP_INTERNAL_API_BASE",
    "http://localhost:8000/api",
).rstrip("/")

# Framework agent API base.
_AGENT_API = f"{_INTERNAL_API_BASE}/agent"

# Tool-config API base — the per-agent tool whitelist endpoints.
_TOOLS_API = f"{_INTERNAL_API_BASE}/agents"

# Session API base — used to ensure a target agent has a session before
# skill operations (skill endpoints resolve the workspace via session).
_SESSIONS_API = f"{_INTERNAL_API_BASE}/sessions"

# Skill API base — external skillhub catalog + download endpoints.
_SKILLS_API = f"{_INTERNAL_API_BASE}/workspace"

# 注：workspace builtins（``Bash``/``Read``/...）与普通工具**同等可配置**——
# 它们参与白名单 diff，可被启用或停用。名称/元数据统一取自
# ``bocomadp.tool_catalog``，避免与运行时大写名不一致（历史 bug）。

#: Unicode 连字符/空白 → ASCII 映射（LLM 生成的名称中很常见）。
_NAME_TRANS = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\u00a0": " ",  # non-breaking space
    },
)


def _clean_name(name: str) -> str:
    """清洗智能体名称：Unicode 连字符/空白归一化为 ASCII。

    LLM 生成的名称常含 U+2011（不间断连字符）等字符；这些字符一旦
    被拼进 agent_id、目录名或 K8s label，会触发 API server 422。
    归一化只影响显示名称中的特殊连字符，可读性不变。
    """
    return name.translate(_NAME_TRANS).strip()


def init_factory_tools(
    tool_registry: Any = None,
    mcp_registry: Any = None,
) -> None:
    """Wire the factory tools to live registries.

    Call once at startup, before any agent-creator conversation.

    Args:
        tool_registry: :class:`ToolRegistry` instance.
        mcp_registry: :class:`McpRegistry` instance.
    """
    global _tool_registry, _mcp_registry  # noqa: PLW0603
    _tool_registry = tool_registry
    _mcp_registry = mcp_registry
    logger.info(
        "agent_factory_tools initialized: tools=%d mcps=%d",
        len(_tool_registry.list_tool_names()) if _tool_registry else 0,
        len(_mcp_registry.list_mcps()) if _mcp_registry else 0,
    )


def _known_tool_names() -> set[str]:
    """Return the configurable tool set M.

    Mirrors :func:`bocomadp.routers.agent_tools._all_tool_names` so the
    factory tools validate names against the very universe the tool
    config APIs accept (builtins + registry + MCP + framework +
    enterprise).
    """
    names: set[str] = set(BUILTIN_TOOL_NAMES)
    names.update(m["name"] for m in FRAMEWORK_TOOLS_META)
    names.update(m["name"] for m in enterprise_tools_meta())
    if _tool_registry is not None:
        try:
            names.update(_tool_registry.list_tool_names())
        except Exception:  # noqa: BLE001
            logger.debug("list_tool_names failed", exc_info=True)
    if _mcp_registry is not None:
        try:
            for mcp in _mcp_registry.list_mcps():
                name = getattr(mcp, "name", "") or ""
                if name:
                    names.add(name)
        except Exception:  # noqa: BLE001
            logger.debug("list_mcps failed", exc_info=True)
    return names


def _normalize_tool_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Normalize (case) and validate a requested tool-name list.

    Returns:
        tuple[list[str], list[str]]: ``(valid, unknown)`` — ``valid`` is
        the de-duplicated canonical name list; ``unknown`` holds names
        that are not part of the configurable set M.
    """
    canonical = [canonical_tool_name(n) for n in names or []]
    canonical = [n for n in canonical if n]
    known = _known_tool_names()
    unknown = sorted({n for n in canonical if n not in known})
    valid = sorted({n for n in canonical if n in known})
    return valid, unknown


def _tool_state(payload: dict[str, Any]) -> tuple[set[str], set[str]]:
    """解析 ``GET /agents/{agent_id}/tools`` 的响应。

    Returns:
        tuple[set[str], set[str]]: ``(enabled, all_names)`` —— ``enabled``
        是当前处于启用状态的可配置工具名；``all_names`` 是接口报告的全部
        可配置工具名（即可配置集合 M）。展示用的工厂工具
        （``toggleable=False``）两边都不计入，因为它们不在白名单里，
        对它们发 PUT/DELETE 会 404。
    """
    enabled: set[str] = set()
    all_names: set[str] = set()
    for tool in payload.get("tools", []):
        if not tool.get("toggleable", True):
            continue
        name = tool.get("name", "")
        if not name:
            continue
        all_names.add(name)
        if tool.get("enabled"):
            enabled.add(name)
    for mcp in payload.get("mcps", []):
        name = mcp.get("name", "")
        if not name:
            continue
        all_names.add(name)
        if mcp.get("enabled"):
            enabled.add(name)
    return enabled, all_names


#: list_tools_for_agent 里每个工具简介的最大字符数。
_TOOL_BRIEF_LIMIT = 50


def _brief(description: str, name: str = "", limit: int = _TOOL_BRIEF_LIMIT) -> str:
    """把工具的长描述压成一句话（供 list_tools_for_agent 展示）。

    规则：折叠空白 → 剥掉开头与工具名重复的部分（含"工具"/"tool"后缀）
    → 取第一句 → 超长则在最近的标点处截断，不硬切词。

    Args:
        description (str): 原始描述，可能含多段换行长文。
        name (str): 工具名，用于剥掉描述开头重复的名字。
        limit (int): 单行最大字符数。

    Returns:
        str: 压好的一句话；原描述为空时返回空串。
    """
    text = " ".join((description or "").split())
    if not text:
        return ""
    if name and text.startswith(name):
        text = text[len(name):].lstrip()
        for suffix in ("工具：", "工具:", "工具", "tool:", "tool：", "tool"):
            if text.lower().startswith(suffix.lower()):
                text = text[len(suffix):]
                break
        text = text.lstrip("：: ")
    if not text:
        return ""

    head = text
    for idx, ch in enumerate(text):
        if ch in "。！？!?":
            head = text[: idx + 1]
            break
    if len(head) <= limit:
        return head

    window = head[:limit]
    cut = max(window.rfind(p) for p in "，,、；;")
    if cut >= limit // 2:  # 断点太靠前（语义不完整），宁可硬截
        return window[: cut + 1] + "…"
    return window.rstrip() + "…"


def _project_tools_meta() -> list[tuple[str, str]]:
    """Return ``[(name, description), ...]`` for registry-provided tools.

    Prefers :meth:`ToolRegistry.list_tools`（带 description）；注册表不提供
    或调用失败时回退到 :meth:`ToolRegistry.list_tool_names`（只有名字）。

    Returns:
        list[tuple[str, str]]: ``(工具名, 描述)`` 列表，描述可能为空串。
    """
    if _tool_registry is None:
        return []
    try:
        tools = list(_tool_registry.list_tools())
    except Exception:  # noqa: BLE001 —— 注册表异常不影响其余目录
        logger.debug("list_tools failed; falling back to names", exc_info=True)
        tools = []
    metas: list[tuple[str, str]] = []
    for tool in tools:
        name = getattr(tool, "name", "") or ""
        if not name:
            continue
        metas.append((name, getattr(tool, "description", "") or ""))
    if metas:
        return metas
    return [(n, "") for n in _tool_registry.list_tool_names()]


async def _ensure_editable_agent(agent_id: str, action: str = "修改") -> str:
    """校验 *agent_id* 对当前调用者存在且可编辑，通过返回空字符串。

    通过 ``GET /agent``（列表接口按调用者归属过滤，内含 ``editable``
    标记）确认目标可见性与编辑权。**不能**仅凭 id 直接改：
    ``_BuiltinAgentStorageProxy.get_agent`` 对 ``default`` 名下智能体做了
    无条件兜底，会让内置/他人智能体在编辑权解析中被误判为"自己的"。

    Args:
        agent_id (str): 目标智能体 ID。
        action (str): 失败提示中使用的动作词（``"修改"`` / ``"删除"``）。

    Returns:
        str: 空字符串表示校验通过；否则为给模型看的错误说明。
    """
    # 系统内置智能体（_agent-creator 等）由服务端托管，禁止改。
    if agent_id.startswith("_"):
        return f"智能体 '{agent_id}' 是系统内置的，不可{action}。"

    agents = await _list_agents()
    if isinstance(agents, str):
        return agents
    for agent in agents:
        # agent_id 取列表响应的**顶层 ``id``**（自动翻页后为全量）。
        if agent.get("id") != agent_id:
            continue
        if agent.get("editable"):
            return ""
        return f"智能体 '{agent_id}' 对当前用户只读，无法{action}。"
    return (
        f"智能体 '{agent_id}' 不存在或无权访问。"
        "调用 list_agents 查看当前用户可管理的智能体。"
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _api(
    method: str,
    path: str,
    body: dict | None = None,
    base: str = _AGENT_API,
) -> dict | str:
    """Call a framework REST API and return parsed JSON or an error
    string.

    Async on purpose: factory tools run inside the uvicorn event loop,
    and a synchronous ``urllib`` call to this same server deadlocks —
    the loop cannot serve the request until the tool returns, but the
    tool waits for the response (10s timeout, then the agent is still
    created and the whitelist write is silently skipped).
    """
    url = f"{base}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-User-ID": _current_user_id.get(),
        "guwpToken": _current_token.get(),
    }
    # 完整请求日志（凭据打码，避免 token 落盘/进日志收集）。
    logger.debug(
        "[factory-api] --> %s %s\n  headers=%s\n  body=%s",
        method,
        url,
        {
            k: ("***redacted***" if k.lower() == "guwptoken" and v else v)
            for k, v in headers.items()
        },
        json.dumps(body, ensure_ascii=False) if body is not None else None,
    )
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.request(
                method,
                url,
                json=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.debug(
            "[factory-api] !! %s %s raised %s: %s",
            method,
            url,
            type(exc).__name__,
            exc,
        )
        return f"无法连接 Agent API: {exc}"

    # 原始响应日志（含重定向后的最终 URL / 状态码 / 未解析响应体）。
    logger.debug(
        "[factory-api] <-- %s %s\n  final_url=%s status=%s\n  body=%s",
        method,
        url,
        resp.request.url,
        resp.status_code,
        resp.text,
    )

    if resp.status_code == 204:  # DELETE returns no content
        return {}
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return f"请求失败 (HTTP {resp.status_code}): {detail}"
    return resp.json()


#: 智能体列表接口的翻页大小。服务端（bocomadp 版 ``GET /agent``）每页默认
#: 只返回 5 条（``pageSize`` 默认值，上限 100），工厂工具必须显式翻页，
#: 否则只能看到前 5 个智能体，其余会"看起来不存在"。
_AGENT_LIST_PAGE_SIZE = 100

#: 翻页防御上限（100 页 × 100 条 = 1 万条），避免服务端异常时死循环。
_AGENT_LIST_MAX_PAGES = 100


async def _list_agents() -> list[dict[str, Any]] | str:
    """拉取当前用户可见的**全部**智能体条目（自动翻页）。

    ``GET /agent`` 默认每页 5 条，这里显式按 :data:`_AGENT_LIST_PAGE_SIZE`
    翻页直到取完，返回值即 ``ListAgentsResponse.agents`` 的并集。

    注意：条目里的智能体 id 在**顶层 ``id``** 字段（不是 ``agent_id``，
    也不是 ``data.id`` —— 后者是 ``AgentData`` 自己的随机 id）。

    Returns:
        list[dict[str, Any]] | str: 条目列表；请求失败时返回错误字符串
        （与 :func:`_api` 的约定一致，调用方用 ``isinstance(..., str)``
        判断）。
    """
    agents: list[dict[str, Any]] = []
    page = 1
    while True:
        result = await _api(
            "GET",
            f"/?pageNum={page}&pageSize={_AGENT_LIST_PAGE_SIZE}",
        )
        if isinstance(result, str):
            return result
        batch = result.get("agents") or []
        agents.extend(batch)
        # 不足一页即已取完；不依赖 total，兼容不带 total 的实现。
        if len(batch) < _AGENT_LIST_PAGE_SIZE:
            break
        total = result.get("total")
        if isinstance(total, int) and len(agents) >= total:
            break
        page += 1
        if page > _AGENT_LIST_MAX_PAGES:
            logger.warning(
                "agent list pagination stopped at %d pages (%d agents)",
                _AGENT_LIST_MAX_PAGES,
                len(agents),
            )
            break
    return agents


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
async def create_agent(
    name: str,
    system_prompt: str,
    max_iters: int = 20,
    enabled_tools: list[str] = [],
) -> str:
    """创建一个新的智能体配置。

    智能体 ID 由系统自动生成（返回结果中会包含）。创建成功后返回完整
    配置信息。

    Args:
        name (str): 显示名称，如 '客服助手'
        system_prompt (str): 决定智能体行为的核心提示词
        max_iters (int): 最大推理轮次（默认20，复杂任务可设30~50）
        enabled_tools (list[str]): 要启用的工具名列表；空列表表示全部可用。
            工具名从 list_tools_for_agent 的结果中选取（大小写不敏感，
            但必须是其中的名字，否则创建失败并返回非法名清单）。
    """
    name = _clean_name(name)

    # Validate/normalize the requested tool names *before* creating, so an
    # invalid or mis-cased name never lands in the whitelist (a whitelist
    # entry that matches nothing would silently strip every other tool).
    requested, unknown = _normalize_tool_names(enabled_tools)
    if unknown:
        return (
            "以下工具名不存在，未创建智能体。请先调用 list_tools_for_agent "
            "获取可用工具名（注意大小写）：\n- " + "\n- ".join(unknown)
        )

    body: dict = {
        "name": name,
        "system_prompt": system_prompt,
        "react_config": {"max_iters": max_iters},
    }
    result = await _api("POST", "/", body)
    if isinstance(result, str):
        return result
    agent_id = result.get("agent_id", "")
    if not agent_id:
        return json.dumps(result, ensure_ascii=False, indent=2)

    # Tool whitelist — build the whitelist directly for the requested
    # tools. The PUT endpoint's semantics are「empty list = all enabled」,
    # so per-tool PUT calls are no-ops on a fresh agent; writing the
    # whitelist directly makes a non-empty ``enabled_tools`` actually
    # restrict the agent to exactly those tools at runtime.
    if requested:
        from bocomadp.routers.agent_tools import _set_enabled_tools

        _set_enabled_tools(agent_id, list(requested))

    lines = [f"智能体 '{name}' 创建成功，agent_id: {agent_id}"]
    if requested:
        lines.append(f"已启用工具: {', '.join(requested)}")
    else:
        lines.append("工具配置: 全部可用")
    return "\n".join(lines)


@tool
async def update_agent(
    agent_id: str,
    name: str = "",
    system_prompt: str = "",
    max_iters: int | None = None,
) -> str:
    """修改已有智能体的配置。未传入的字段保持原值不变。

    先调用 get_agent 查看当前配置，再决定修改哪些字段。本工具会先校验
    目标智能体存在且对当前用户可编辑；系统内置智能体（``_`` 开头）不可
    修改。

    Args:
        agent_id (str): 要修改的智能体 ID（系统生成的 UUID）
        name (str): 新的显示名称（空字符串表示不改）
        system_prompt (str): 新的系统提示词（空字符串表示不改）
        max_iters (int | None): 新的最大轮次（None表示不改）
    """
    body: dict = {}
    if name:
        body["name"] = _clean_name(name)
    if system_prompt:
        body["system_prompt"] = system_prompt
    if max_iters is not None:
        body["react_config"] = {"max_iters": max_iters}

    if not body:
        return "未提供任何要修改的字段。"

    error = await _ensure_editable_agent(agent_id)
    if error:
        return error

    result = await _api("PATCH", f"/{agent_id}", body)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2)


@tool
async def delete_agent(agent_id: str) -> str:
    """删除一个智能体配置。系统内置的智能体不可删除。

    与 update_agent 一致：先校验目标智能体对当前用户存在且有编辑权，
    校验不通过（内置智能体 / 只读 / 不存在或无权访问）时直接返回说明，
    **不发起删除请求**。删除会级联清理该智能体的会话、工具白名单与记忆
    记录，不可恢复。

    Args:
        agent_id (str): 要删除的智能体 ID（系统生成的 UUID）
    """
    # 归属/编辑权预检（含内置智能体拦截）。不能只依赖服务端：
    # _BuiltinAgentStorageProxy.get_agent 对 default 名下智能体做了无条件
    # 兜底，会让 resolve_for_edit 误判为"自己的"，从而放行删除。
    error = await _ensure_editable_agent(agent_id, action="删除")
    if error:
        return error

    result = await _api("DELETE", f"/{agent_id}")
    if isinstance(result, str):
        return result
    return f"智能体 '{agent_id}' 已删除。"


@tool
async def list_agents() -> str:
    """列出当前用户可见的智能体（自己的 + 被共享的），一行一个。

    每行格式：``agent_id | 名称 | 可编辑(✓/✗)``。修改/删除智能体前先用它
    定位目标 agent_id；需要完整 system prompt 等信息再用 get_agent。
    不含团队成员的 worker 智能体。
    """
    agents = await _list_agents()
    if isinstance(agents, str):
        return agents
    if not agents:
        return "当前还没有创建任何智能体。调用 create_agent 来创建第一个吧。"

    lines = [f"共 {len(agents)} 个智能体（agent_id | 名称 | 可编辑）:"]
    for a in agents:
        # 智能体 id 是列表条目的顶层 ``id``（自动翻页后为全量）。
        data = a.get("data") or {}
        lines.append(
            f"{a.get('id', '')} | {data.get('name', '')} | "
            f"{'✓' if a.get('editable') else '✗'}",
        )
    return "\n".join(lines)


@tool
async def get_agent(agent_id: str) -> str:
    """查看指定智能体可配置项与完整 system prompt。

    只返回工厂能改/需要看的字段：``agent_id`` / 名称 / ``max_iters`` /
    是否可编辑 / 完整 ``system_prompt``。上下文压缩、邀请等不可改配置
    不返回；工具配置用 get_agent_tools。

    Args:
        agent_id (str): 智能体 ID（系统生成的 UUID）
    """
    # 框架没有单查接口 —— 拉全量列表再本地过滤（_list_agents 已自动翻页）。
    agents = await _list_agents()
    if isinstance(agents, str):
        return agents

    for a in agents:
        if a.get("id") != agent_id:
            continue
        data = a.get("data") or {}
        react = data.get("react_config") or {}
        return "\n".join(
            [
                f"agent_id: {agent_id}",
                f"名称: {data.get('name', '')}",
                f"max_iters: {react.get('max_iters', 20)}",
                f"可编辑: {'✓' if a.get('editable') else '✗'}",
                "system_prompt:",
                data.get("system_prompt", "") or "",
            ],
        )

    return (
        f"智能体 '{agent_id}' 不存在。"
        f"调用 list_agents 查看所有已创建的智能体。"
    )


@tool
async def get_agent_tools(agent_id: str) -> str:
    """查看指定智能体当前启用了哪些工具（可配置集合内的启用/停用清单）。

    ``get_agent`` 只返回基础配置（名称/prompt/轮次），**不含工具**；要了解
    某个智能体的工具现状，用本工具。修改工具前应先调用它确认现状，再用
    ``set_agent_tools`` 做覆盖式调整。

    Args:
        agent_id (str): 目标智能体 ID
    """
    result = await _api("GET", f"/{agent_id}/tools", base=_TOOLS_API)
    if isinstance(result, str):
        return result

    enabled: list[str] = []
    disabled: list[str] = []
    readonly: list[str] = []
    for tool in result.get("tools", []):
        name = tool.get("name", "")
        if not name:
            continue
        if not tool.get("toggleable", True):
            readonly.append(name)
        elif tool.get("enabled"):
            enabled.append(name)
        else:
            disabled.append(name)

    def _join(names: list[str]) -> str:
        return ", ".join(sorted(names)) or "（无）"

    lines = [
        f"智能体 '{agent_id}' 的工具配置：",
        "",
        f"已启用（{len(enabled)}）: {_join(enabled)}",
        f"未启用（{len(disabled)}）: {_join(disabled)}",
    ]
    if readonly:
        lines.append(f"仅展示不可配置（{len(readonly)}）: {_join(readonly)}")

    mcps = result.get("mcps", [])
    if mcps:
        lines.append(
            "MCP: "
            + ", ".join(
                f"{m.get('name', '')}"
                f"[{'已启用' if m.get('enabled') else '未启用'}]"
                for m in mcps
            ),
        )

    if not disabled:
        lines += ["", "说明：未启用为空 = 当前为「全部可用」。"]
    return "\n".join(lines)


@tool
def list_tools_for_agent() -> str:
    """列出系统中所有可分配给智能体的工具和MCP服务器。

    输出的就是**可配置工具集合**（框架内置 / 项目工具 / 框架团队与规划 /
    企业工具 / MCP 服务器），其中的工具名可直接用于 create_agent 的
    ``enabled_tools`` 或 set_agent_tools，大小写需保持一致。

    每项只给一句话简介（长描述会被压成一行）；工具的详细用法由目标智能体
    在运行时自行探索。
    """
    lines: list[str] = ["# 可配置工具与 MCP", "", "## 框架内置工具（文件 / 命令）"]

    # 1. workspace builtins（运行时真值：首字母大写）
    for meta in BUILTIN_TOOLS_META:
        name = meta["name"]
        lines.append(f"- {name}: {_brief(meta['description'], name)}")

    # 2. 项目工具
    project_tools = _project_tools_meta()
    if project_tools:
        lines += ["", "## 项目工具"]
        for name, desc in project_tools:
            lines.append(f"- {name}: {_brief(desc, name)}" if desc else f"- {name}")

    # 3. 框架团队/规划工具
    lines += ["", "## 框架团队 / 规划工具（多智能体协作与任务规划）"]
    for meta in FRAMEWORK_TOOLS_META:
        name = meta["name"]
        lines.append(f"- {name}: {_brief(meta['description'], name)}")

    # 4. 企业工具
    enterprise_tools = enterprise_tools_meta()
    if enterprise_tools:
        lines += ["", "## 企业工具"]
        for meta in enterprise_tools:
            name = meta.get("name", "")
            desc = meta.get("description", "")
            lines.append(f"- {name}: {_brief(desc, name)}" if desc else f"- {name}")

    # 5. MCP 服务器
    if _mcp_registry is not None:
        mcps = _mcp_registry.list_mcps()
        if mcps:
            lines += ["", "## MCP 服务器"]
            for mcp in mcps:
                mcp_name = getattr(mcp, "name", "") or ""
                mcp_desc = getattr(mcp, "description", None) or ""
                lines.append(
                    f"- {mcp_name}: {_brief(mcp_desc)}" if mcp_desc
                    else f"- {mcp_name}",
                )

    return "\n".join(lines)


@tool
async def set_agent_tools(
    agent_id: str,
    enabled_tools: list[str],
) -> str:
    """全量设置智能体的工具白名单（覆盖式）。

    - enabled_tools 为空列表：全部工具可用
    - enabled_tools 非空：只启用列表中的工具（按名称精确匹配）

    内置工具（Bash/Read/Write/Edit/Glob/Grep）与其它工具同等对待，
    可以启用也可以停用。工具名从 list_tools_for_agent 选取，大小写
    不敏感但需存在于可配置集合中；**传入不存在的名字会直接拒绝且
    不做任何修改**。

    与 update_agent 一样，本工具会先校验目标智能体存在且对当前用户可
    编辑；系统内置智能体（``_`` 开头）不可修改。

    Args:
        agent_id (str): 目标智能体 ID
        enabled_tools (list[str]): 工具名列表（从 list_tools_for_agent 选取）
    """
    error = await _ensure_editable_agent(agent_id)
    if error:
        return error

    # 工具名前置校验（与 create_agent 一致）：非法名直接拒绝，避免
    # "先删掉旧工具、再发现新名字不存在"这种带副作用的半成品失败。
    target_list, unknown = _normalize_tool_names(enabled_tools)
    if unknown:
        return (
            "以下工具名不存在，未做任何修改。请先调用 list_tools_for_agent "
            "获取可用工具名（注意大小写）：\n- " + "\n- ".join(unknown)
        )

    # 1. Read current enabled state
    result = await _api("GET", f"/{agent_id}/tools", base=_TOOLS_API)
    if isinstance(result, str):
        return result
    current_enabled, all_names = _tool_state(result)

    target = set(target_list)
    errors: list[str] = []

    # 2. Diff-align.
    if not target:
        # 空目标 = 全部可用：逐个停用当前已启用的工具，最后一次移除
        # 落到空白名单，服务端语义即为「全部可用」。
        if current_enabled == all_names:
            return f"智能体 '{agent_id}' 的工具已是全部可用。"
        for name in sorted(current_enabled):
            r = await _api("DELETE", f"/{agent_id}/tools/{name}", base=_TOOLS_API)
            if isinstance(r, str):
                errors.append(r)
    else:
        # **先加后删**：先把目标工具写进白名单，删除阶段就不可能把它
        # 们删空。若反过来（先删后加），一旦「当前已启用」与目标无交集，
        # 最后一个 DELETE 会把白名单写成 []，被服务端理解为「全部可用」，
        # 于是后续 PUT 全部变成空操作 —— 结果是静默放开所有工具，却仍
        # 返回成功文案。
        for name in sorted(target - current_enabled):
            r = await _api("PUT", f"/{agent_id}/tools/{name}", base=_TOOLS_API)
            if isinstance(r, str):
                errors.append(r)
        for name in sorted(current_enabled - target):
            r = await _api("DELETE", f"/{agent_id}/tools/{name}", base=_TOOLS_API)
            if isinstance(r, str):
                errors.append(r)

    if errors:
        return "工具配置部分失败:\n" + "\n".join(errors)

    # 3. 回读校验：确认最终状态与目标一致。防止"报成功但实际不符"
    # （如服务端与工具侧的工具集合不一致，导致某些 PUT/DELETE 未生效）。
    check = await _api("GET", f"/{agent_id}/tools", base=_TOOLS_API)
    if isinstance(check, str):
        return check
    final_enabled, final_all = _tool_state(check)
    expected = target if target else final_all
    if final_enabled != expected:
        return (
            f"工具配置未生效：期望 {', '.join(sorted(expected))}，"
            f"实际 {', '.join(sorted(final_enabled))}。"
            "请重试，或用工具面板核对。"
        )

    if not target:
        return f"智能体 '{agent_id}' 的工具已设置为全部可用。"
    return (
        f"智能体 '{agent_id}' 的工具白名单已设置为: "
        f"{', '.join(sorted(target))}。"
    )


async def _get_or_create_session(agent_id: str) -> tuple[str, str]:
    """Ensure *agent_id* has at least one session.

    Skill endpoints resolve the target workspace through a session
    record, so a session must exist before any skill operation.

    Returns:
        ``(session_id, error)`` — exactly one of the two is non-empty.
    """
    # 注意尾斜杠：框架路由定义 GET "/"（/sessions/），请求无尾斜杠的
    # /sessions 会触发 307 重定向；虽然 _api 已开启 follow_redirects，
    # 这里仍显式带上尾斜杠，避免依赖重定向语义。
    result = await _api(
        "GET",
        f"/?agent_id={urllib.parse.quote(agent_id)}",
        base=_SESSIONS_API,
    )
    if isinstance(result, str):
        return "", result

    sessions = result.get("sessions", [])
    if sessions:
        session = sessions[0].get("session", {}) or {}
        sid = session.get("id", "")
        if sid:
            return sid, ""

    created = await _api("POST", "/", {"agent_id": agent_id}, base=_SESSIONS_API)
    if isinstance(created, str):
        return "", created
    return created.get("session_id", ""), ""


@tool
async def list_available_skills(agent_id: str, keyword: str = "") -> str:
    """查看技能市场中可用的技能列表。

    Args:
        agent_id (str): 目标智能体 ID
        keyword (str): 可选关键词，按技能名/描述过滤
    """
    session_id, err = await _get_or_create_session(agent_id)
    if err:
        return err

    params = urllib.parse.urlencode({
        "agent_id": agent_id,
        "session_id": session_id,
        "q": keyword,
    })
    result = await _api("GET", f"/skills/external?{params}", base=_SKILLS_API)
    if isinstance(result, str):
        return result

    skills = result.get("skills", [])
    if not skills:
        return "技能市场暂无可用技能。"

    lines = [f"共 {len(skills)} 个技能（used=已安装）:\n"]
    for s in skills:
        name = s.get("name", "")
        category = s.get("category", "")
        desc = (s.get("description", "") or "")[:60]
        used = "✓已安装" if s.get("used") else "未安装"
        lines.append(f"- {category}:{name}  [{used}]  {desc}")
    return "\n".join(lines)


@tool
async def enable_skill_for_agent(agent_id: str, skill_full_name: str) -> str:
    """为智能体安装（启用）一个技能。

    Args:
        agent_id (str): 目标智能体 ID
        skill_full_name (str): 技能全名，格式 'category:name'
            （如 'public:writing'），从 list_available_skills 的结果中选取
    """
    session_id, err = await _get_or_create_session(agent_id)
    if err:
        return err

    params = urllib.parse.urlencode({
        "agent_id": agent_id,
        "session_id": session_id,
    })
    result = await _api(
        "POST",
        f"/skill/download/{urllib.parse.quote(skill_full_name, safe=':')}"
        f"?{params}",
        base=_SKILLS_API,
    )
    if isinstance(result, str):
        return result
    if result.get("success"):
        return f"技能 '{skill_full_name}' 已安装到智能体 '{agent_id}'。"
    return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = [
    "init_factory_tools",
    "_current_user_id",
    "_current_token",
    "create_agent",
    "update_agent",
    "delete_agent",
    "list_agents",
    "get_agent",
    "get_agent_tools",
    "list_tools_for_agent",
    "set_agent_tools",
    "list_available_skills",
    "enable_skill_for_agent",
    "_current_session_id",
]
