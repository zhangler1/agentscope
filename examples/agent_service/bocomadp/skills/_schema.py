# -*- coding: utf-8 -*-
"""外部 skill 端点请求 / 响应模型（迁移自 ``bankcomm_adp.routers._schema``）。"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 技能引用（``namespace:name``）
# ---------------------------------------------------------------------------
# 注意：引用里 ``:`` 前的部分只是**远端 namespace**（``global`` / ``bocom``…），
# 安装一律走 external skillhub，不会按前缀切换 hub。

#: 技能引用格式：``namespace:name``。
#: - ``namespace`` 限 ASCII（远端 namespace/slug 都是 ASCII）；
#: - ``name`` 允许中文（bocom 技能名含中文），但**排除** ``/`` 与 ``\\``
#:   ——它会进 tar 解压路径与沙箱目录名，必须挡住路径穿越。
_SKILL_REF_RE = re.compile(
    r"^(?P<namespace>[A-Za-z0-9_.\-]{1,64}):(?P<name>[^:/\x00\\]{1,128})$",
)

#: ``POST /workspace/skill/ensure`` 单次最多处理多少个引用。
#: 每一项都是一次"远端下载 + 沙箱写入"的串行往返，必须设上限（模板侧
#: ``agent_template`` 的上限是 50，这里给 2 倍余量），否则一次请求可能
#: 打满网关超时。
ENSURE_SKILLS_MAX = 100


def parse_skill_ref(ref: str) -> tuple[str, str]:
    """把技能引用 ``namespace:name`` 拆成 ``(namespace, name)``。

    与 ``skill_router`` 的 full name 口径一致（``global:rollback-check-sql``、
    ``bocom:excel智能分析``）。

    Args:
        ref (`str`):
            原始引用；允许首尾空白。

    Returns:
        `tuple[str, str]`:
            ``(namespace, name)``。

    Raises:
        `ValueError`:
            格式不是 ``namespace:name``、含路径穿越（``..``）或含 ``/``。
    """
    text = (ref or "").strip()
    match = _SKILL_REF_RE.match(text)
    if match is None or ".." in text:
        raise ValueError(
            "skill ref must be 'namespace:name' (e.g. "
            "'global:rollback-check-sql'); got "
            f"{ref!r}",
        )
    return match.group("namespace"), match.group("name")


class SkillInfo(BaseModel):
    """One skill listing served by the agent-skills endpoint."""

    name: str = Field(description="The skill name (slug).")
    category: str = Field(default="public", description="The skill category.")
    description: str = Field(
        default="",
        description="The user-facing description of the skill.",
    )
    version: str = Field(
        default="",
        description=(
            "The skill's **published** version, taken from the catalog item's "
            "``publishedVersion.version`` (e.g. ``v20260907.102346``). Empty "
            "string when the remote reports no published version (``null``)."
        ),
    )
    used: bool = Field(
        default=False,
        description="Whether the caller already installed this skill.",
    )


class AgentSkillsListResponse(BaseModel):
    """Response body for the external skill list endpoints."""

    skills: list[SkillInfo] = Field(
        default_factory=list,
        description="The skills on this page.",
    )
    total: int = Field(
        default=0,
        description="The number of skills returned.",
    )


class SkillActionResponse(BaseModel):
    """Response body for the skill enable/download action."""

    success: bool = Field(description="Whether the action succeeded.")
    action: str = Field(description="The action performed, e.g. 'enabled'.")
    skill_id: str = Field(
        description="The skill identifier, 'category:name'.",
    )


class EnsureSkillsRequest(BaseModel):
    """``POST /workspace/skill/ensure`` 请求体。

    元素为技能引用 ``namespace:name``（如 ``global:rollback-check-sql``、
    ``bocom:excel智能分析``），通常直接来自
    ``GET /agent/template/agents`` 或 ``POST /agent/{id}/copy`` 返回的
    ``skills`` 字段。非法引用（缺 ``namespace``、含 ``/`` 或 ``..``）
    在模型层就抛 ``ValidationError`` → FastAPI 422。
    """

    skills: list[str] = Field(
        default_factory=list,
        max_length=ENSURE_SKILLS_MAX,
        description=(
            "期望安装的技能引用列表，元素形如 ``namespace:name``。"
            "安装一律走 **external skillhub**（``ns`` 原样作为远端 namespace "
            f"透传，``global`` / ``bocom`` 都只是 namespace，不切换 hub）；"
            f"最多 {ENSURE_SKILLS_MAX} 项（每项一次远端下载 + 沙箱写入，"
            "串行执行）。空列表 = 不做任何安装，只返回当前清单。"
        ),
    )

    @field_validator("skills")
    @classmethod
    def _check_refs(cls, value: list[str]) -> list[str]:
        """逐项校验并把引用规范化为 ``namespace:name``（strip 后回写）。"""
        cleaned: list[str] = []
        for raw in value:
            try:
                namespace, name = parse_skill_ref(raw)
            except ValueError as e:
                raise ValueError(str(e)) from None
            cleaned.append(f"{namespace}:{name}")
        return cleaned
