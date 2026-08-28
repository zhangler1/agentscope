# -*- coding: utf-8 -*-
"""CustomPromptMiddleware —— 请求级自定义提示词整体覆盖 + 公共提示词占位符注入。

deer-flow 在 agent 构建时用 ``custom_params["custom_prompt"]`` **整体替换**
``system_prompt``（绕过模板提示词）；bocomadp 对齐这一语义，实现
AgentScope 的 ``on_system_prompt`` transformer 钩子，并在其上叠加
**PostgreSQL 公共提示词占位符注入**：

- 框架每次模型调用前经 ``Agent._get_system_prompt`` 组装 system 提示词
  （config 的 agent 级 system_prompt + skill 指令 + workspace 指令拼接），
  然后**依次应用**实现了 ``on_system_prompt`` 的中间件，返回值为最终
  提示词（transformer 模式，见 ``agentscope/agent/_agent.py``）；
- custom_params 携带非空 ``custom_prompt`` → 直接返回它，**整体覆盖**
  （与 deer-flow 等价）；
- 否则从 PostgreSQL ``system_prompts`` 表读取公共提示词（该智能体 →
  全局回退），把其中的 ``<技能注入>`` / ``<工作区>`` 占位符替换为框架拼好的
  ``<agent-skills>`` / ``<workspace>`` 段；没有占位符的段追加到末尾；
  数据库无提示词时原样透传。

存储迁移说明：公共提示词已由 Redis 迁移到 PostgreSQL（持久化真源），
写入端为管理 API ``routers/system_prompt.py``，读取端为本中间件。
两者共用 ``system_prompts`` 表（``config_key`` = agent_id 或 ``global``）。

历史教训：早期版本用 ``on_reply`` 做消息级注入，但该钩子的
``input_kwargs`` 仅含 ``inputs`` / ``structured_schema``（消息在
``_reply_impl`` 内组装），注入逻辑从未生效；``on_system_prompt``
才是提示词覆盖的正确落点。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

try:
    from bocomadp.middleware.agent_middleware import MiddlewareBase
except Exception:  # pragma: no cover - agentscope 不可用时降级（如纯单测环境）
    class MiddlewareBase:  # type: ignore
        """最小兜底基类：仅在 AgentScope 不可用时使用，保证可导入。"""

        async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
            return current_prompt

from bocomadp.deerflow.custom_params import get_custom_params

logger = logging.getLogger(__name__)

# ── 占位符常量 ──
_SKILLS_PLACEHOLDER = "<技能注入>"
_WORKSPACE_PLACEHOLDER = "<工作区>"
_USER_PROMPT_PLACEHOLDER = "<用户提示词>"

_SECTIONS = ("agent-skills", "workspace")


def _extract_section(text: str, tag: str) -> str:
    """提取 ``<tag>...</tag>`` 段。

    Args:
        text (`str`): 源文本。
        tag (`str`): 标签名（不含尖括号），如 ``"agent-skills"``。

    Returns:
        `str`: 命中的整段（含标签），未命中返回空串。
    """
    m = re.search(rf"<{tag}>.*?</{tag}>", text, re.DOTALL)
    return m.group(0) if m else ""


def _extract_base(text: str) -> str:
    """提取基础提示词（去掉 ``<agent-skills>`` / ``<workspace>`` 段后的剩余）。

    该部分是 PostgreSQL 中存储的用户输入提示词（``AgentRecord.data.system_prompt``）。
    """
    result = text
    for tag in _SECTIONS:
        section = _extract_section(result, tag)
        if section:
            result = result.replace(section, "")
    return result.strip("\n")


# ── PostgreSQL（持久化真源，替代 Redis） ────────────────────────

_engine: Any = None
_engine_lock = asyncio.Lock()


async def _get_engine() -> Any:
    """懒加载独立 async engine（与框架 storage 同 URL、独立连接池）。

    模式与 ``pool_config.py`` / ``memory_config.py`` 保持一致：
    首次读取时按 ``get_app_config().db.url`` 创建并幂等建表。
    """
    global _engine
    if _engine is None:
        async with _engine_lock:
            if _engine is None:
                from sqlalchemy.ext.asyncio import create_async_engine

                from bocomadp.config import get_app_config

                _engine = create_async_engine(
                    get_app_config().db.url,
                    pool_pre_ping=True,
                )
                await _ensure_table()
    return _engine


async def _ensure_table() -> None:
    """幂等建表（``system_prompts``）。"""
    assert _engine is not None
    from sqlalchemy import text

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS system_prompts ("
                # ``key`` 是 MySQL/OB 保留字（1064 语法错误），用 ``config_key`` 全兼容。
                "config_key VARCHAR(255) PRIMARY KEY, "
                "content TEXT NOT NULL, "
                "updated_at TIMESTAMP NOT NULL"
                ")",
            ),
        )


async def _get_pg_prompt(agent_id: str) -> str:
    """从 PG 读取公共提示词：优先智能体级，缺失回退全局。"""
    try:
        engine = await _get_engine()
        from sqlalchemy import text

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT content FROM system_prompts "
                        "WHERE config_key = :key",
                    ),
                    {"key": agent_id},
                )
            ).first()
            if row is None:
                row = (
                    await conn.execute(
                        text(
                            "SELECT content FROM system_prompts "
                            "WHERE config_key = :key",
                        ),
                        {"key": "global"},
                    )
                ).first()
        return str(row[0]) if row is not None and row[0] else ""
    except Exception as e:  # pragma: no cover - DB 不可用
        logger.warning(
            "CustomPromptMiddleware: PG read failed, "
            "prompt injection disabled: %s",
            e,
        )
        return ""


class CustomPromptMiddleware(MiddlewareBase):
    """把 custom_params 的 custom_prompt 整体覆盖，否则注入 PG 公共提示词。

    拼接顺序（无 custom_prompt 时）：

    1. PostgreSQL 公共提示词（该智能体 → 全局回退），占位符被替换为框架段；
    2. 框架拼好的 ``<agent-skills>`` 段（无占位符时追加）；
    3. 框架拼好的 ``<workspace>`` 段（无占位符时追加）。

    PG 连接懒加载（复用 :func:`get_app_config` 的连接参数），
    首次读取时建立；连接失败静默降级为透传框架结果。
    """

    async def _build_single_skill_section(
        self,
        agent: Any,
        skill_name: str,
    ) -> str:
        """构造只含指定技能的 ``<agent-skills>`` 段（含 SKILL.md 指令）。

        Args:
            agent (`Any`): Agent 实例。
            skill_name (`str`): 技能名（Skill.name）。

        Returns:
            `str`: 单技能的 agent-skills 段；技能不存在或读取失败返回空串。
        """
        workspace = getattr(agent, "offloader", None)
        if workspace is None:
            return ""
        try:
            skills = await workspace.list_skills()
        except Exception as e:  # pragma: no cover
            logger.warning(
                "CustomPromptMiddleware: list_skills failed for "
                "active_skill=%s: %s",
                skill_name,
                e,
            )
            return ""
        for s in skills:
            if getattr(s, "name", "") != skill_name:
                continue
            try:
                backend = workspace.get_backend()
                path = backend.join_path(s.dir, "SKILL.md")
                content = await backend.read_file(path)
                text = (
                    content.decode("utf-8", "replace")
                    if isinstance(content, bytes)
                    else str(content)
                )
            except Exception as e:  # pragma: no cover
                logger.warning(
                    "CustomPromptMiddleware: read SKILL.md failed for "
                    "%s: %s",
                    skill_name,
                    e,
                )
                text = ""
            return (
                "<agent-skills>\n"
                "<skill>\n"
                f"<name>{s.name}</name>\n"
                f"<description>{getattr(s, 'description', '') or ''}</description>\n"
                f"<dir>{s.dir}</dir>\n"
                "</skill>\n"
                "# 技能指令\n"
                f"{text}\n"
                "</agent-skills>"
            )
        return ""

    async def on_system_prompt(self, agent: Any, current_prompt: str) -> str:
        """transformer 钩子：custom_prompt 覆盖 → active_skill 注入 → PG 占位符。"""
        # 1. deer-flow custom_prompt 整体覆盖（保留原逻辑）
        prompt = str(get_custom_params().get("custom_prompt") or "")
        if prompt:
            if prompt != current_prompt:
                logger.info(
                    "CustomPromptMiddleware: custom_prompt overrides "
                    "system prompt (was %d chars, now %d chars)",
                    len(current_prompt),
                    len(prompt),
                )
            return prompt

        # 1.5. 用户指定技能：消息改写由 active_skill.py 负责（把 /skill_name
        #      前缀解析并重写为任务指令）。system prompt 这里**不再**只注入该
        #      技能，而是保留框架默认注入的全部技能（全量 <agent-skills> 段）。
        #      （原 _build_single_skill_section 的单技能过滤逻辑已停用）

        # 2. 从 PostgreSQL 读取公共提示词（该智能体 → 全局回退）
        agent_id = getattr(agent, "name", "") or ""
        pg_prompt = await _get_pg_prompt(agent_id)

        # 3. 无公共提示词 → 原样透传框架结果
        if not pg_prompt:
            return current_prompt

        # 4. 从 current_prompt 提取框架拼好的段
        base = _extract_base(current_prompt)   # PostgreSQL 用户输入提示词
        skills = _extract_section(current_prompt, "agent-skills")
        workspace = _extract_section(current_prompt, "workspace")

        # 5. 拼接：以 PG 公共提示词为主体
        result = pg_prompt

        # 5a. 用户提示词占位符：含 <用户提示词> → 替换；不含 → 追加到末尾
        if base:
            if _USER_PROMPT_PLACEHOLDER in result:
                result = result.replace(_USER_PROMPT_PLACEHOLDER, base)
            else:
                result = result.rstrip("\n") + "\n\n" + base

        # 5b. 技能占位符替换
        if skills:
            result = result.replace(_SKILLS_PLACEHOLDER, skills)
        # 5c. 工作区占位符替换
        if workspace:
            result = result.replace(_WORKSPACE_PLACEHOLDER, workspace)

        # 6. 没有占位符的段 → 补在末尾，避免丢失框架拼好的内容
        missing: list[str] = []
        if skills and _SKILLS_PLACEHOLDER not in pg_prompt:
            missing.append(skills)
        if workspace and _WORKSPACE_PLACEHOLDER not in pg_prompt:
            missing.append(workspace)
        if missing:
            result = result + "\n\n" + "\n\n".join(missing)

        if result != current_prompt:
            logger.info(
                "CustomPromptMiddleware: injected PG public prompt "
                "(was %d chars, now %d chars)",
                len(current_prompt),
                len(result),
            )
        return result
