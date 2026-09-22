# -*- coding: utf-8 -*-
"""Request / response schemas for the BocomADP agent router (expert team)."""
import warnings
from datetime import datetime

from pydantic import BaseModel, Field

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import InviteConfig
from agentscope.app._service import AgentView


class CreateAgentRequest(BaseModel):
    """Request body for creating a new agent."""

    name: str = Field(description="Display name of the agent.")
    description: str = Field(
        default="",
        description=(
            "智能体简介（一句话说明它能做什么）。随 AgentData 存进 "
            "``agents.payload``；不传则空串。"
        ),
    )
    system_prompt: str = Field(
        default="你是一个乐于助人的AI助手。",
        description="Base system prompt fed to the agent.",
    )
    context_config: ContextConfig = Field(
        default_factory=ContextConfig,
        description="Context-window management configuration.",
    )
    react_config: ReActConfig = Field(
        default_factory=ReActConfig,
        description="ReAct loop configuration.",
    )
    invite_config: InviteConfig = Field(
        default_factory=InviteConfig,
        description=(
            "Invite-pool settings for this agent. See "
            ":class:`InviteConfig` — enforces the "
            "``invitable ⇒ non-empty description`` invariant."
        ),
    )
    parent_agent_id: str | None = Field(
        default=None,
        description=(
            "When set, this new agent is created as a member of the "
            "expert team led by the referenced agent. The leader's "
            "team_config.member_ids is updated to include the new agent "
            "automatically. Leave None to create a plain agent."
        ),
    )
    is_team: bool = Field(
        default=False,
        description=(
            "When True (and ``parent_agent_id`` is None), the new agent is "
            "created as an expert-team leader with an empty ``team_config`` "
            "so it is already classified as a team in listings even before "
            "any member references it. Ignored when ``parent_agent_id`` is "
            "set (a member cannot also be a leader)."
        ),
    )


class CreateAgentResponse(BaseModel):
    """Response body after creating an agent."""

    agent_id: str = Field(description="Server-assigned agent identifier.")


class CopyAgentRequest(BaseModel):
    """Request body for copying an agent (payload + 已安装技能)。"""

    name: str | None = Field(
        default=None,
        description=(
            "新智能体名；缺省为 '<源名> 副本'。允许与已有智能体重名。"
        ),
    )
    copy_skills: bool = Field(
        default=True,
        description=(
            "是否同时复制该智能体已安装的技能。仅在 K8s 沙箱部署下生效；"
            "本地模式技能按会话存储，会跳过并在 warnings 中说明。"
        ),
    )


class UpdateAgentRequest(BaseModel):
    """Request body for partially updating an agent.

    Omit any field to keep its current value.
    """

    name: str | None = Field(default=None, description="New display name.")
    description: str | None = Field(
        default=None,
        description=(
            "New description. Omit to keep the current value; pass an "
            "empty string to clear it."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        description="New system prompt.",
    )
    context_config: ContextConfig | None = Field(
        default=None,
        description="New context configuration.",
    )
    react_config: ReActConfig | None = Field(
        default=None,
        description="New ReAct loop configuration.",
    )
    invite_config: InviteConfig | None = Field(
        default=None,
        description=(
            "New invite-pool settings. Pass the full :class:`InviteConfig` "
            "object to update; omit to leave both invitable-related "
            "fields unchanged."
        ),
    )


class TeamAgentView(AgentView):
    """Agent view re-deriving the expert-team fields.

    ``is_team`` / ``parent_agent_id`` / ``is_self_built`` used to live on
    the framework :class:`AgentView`; they were moved out of ``src/`` into
    the ``expert_team_relations`` table. This subclass re-exposes them so
    the wire contract of the list / update endpoints is unchanged.
    """

    is_team: bool = False
    parent_agent_id: str | None = None
    # None at top-level (not a member query); True/False only when the
    # list was queried with ``parent_agent_id`` — mirrors the historical
    # framework ``AgentView`` contract.
    is_self_built: bool | None = None


class CopyAgentResponse(TeamAgentView):
    """Response body for ``POST /agent/{agent_id}/copy``.

    与 ``GET /agent/`` 的列表元素、``PATCH /agent/{id}`` 的响应**同构**
    （都是 :class:`TeamAgentView`）：``id`` / ``created_at`` / ``updated_at``
    / ``user_id`` / ``source`` / ``data`` / ``editable`` / ``is_team`` /
    ``parent_agent_id`` / ``is_self_built`` —— 前端可以把它直接当成一个
    智能体对象插进列表，不需要再调一次 ``GET``。

    不返回复制过程信息（已复制技能名、告警等）：那些只进服务端日志，
    "复制成功"由 HTTP 201 表达。

    - ``editable`` 恒为 ``True``（复制品归属调用者，与 ``PATCH`` 的响应
      口径一致——那两处都是在权限校验之后构造的视图）；
    - 新建的复制品 ``is_team=False`` / ``parent_agent_id=None`` /
      ``is_self_built=None``（团队关系不复制）。
    """

    agent_id: str = Field(
        description=(
            "新智能体 id；与 :attr:`id` 同值，保留以兼容“只取 agent_id”"
            "的旧调用方。"
        ),
    )


class ListAgentsResponse(BaseModel):
    """Response body for listing agents."""

    agents: list[TeamAgentView] = Field(description="Agent records.")
    total: int = Field(description="Total number of agents.")


class OwnedAgentView(BaseModel):
    """一条"我名下拥有"的智能体视图（``GET /agent/owned``）。

    与 ``GET /agent/``（可见性视图：自己的 + 别人共享给我的 + 隐藏
    自建成员）不同，本视图是**纯归属清单**：只含 ``user_id=调用者``
    且 ``source='user'`` 的记录——共享进来的别人的智能体不出现，
    ``source='team'`` 的派生 worker 不出现，**专家团自建成员也不
    返回**（成员独属其团，明细走 ``GET /agent/?parent_agent_id=``）。
    """

    id: str = Field(description="智能体 id。")
    name: str = Field(description="智能体名称。")
    description: str = Field(
        default="",
        description=(
            "智能体简介（AgentData.description）；历史脏数据缺字段时兜底空串。"
        ),
    )
    system_prompt: str = Field(
        default="",
        description=(
            "智能体提示词（AgentData.system_prompt，创建/编辑时填的"
            "那份）；历史脏数据缺字段时兜底空串。"
        ),
    )
    is_team: bool = Field(default=False, description="是否专家团团长。")
    parent_agent_id: str | None = Field(
        default=None,
        description="作为自建成员挂在其名下的团长 id；非团队成员为 null。",
    )
    is_self_built: bool | None = Field(
        default=None,
        description="是否某团长的自建成员；非成员场景为 null。",
    )
    created_at: datetime | None = Field(default=None, description="创建时间。")
    updated_at: datetime | None = Field(default=None, description="最后更新时间。")


class ListOwnedAgentsResponse(BaseModel):
    """Response body for ``GET /agent/owned``."""

    agents: list[OwnedAgentView] = Field(description="本人名下智能体列表。")
    total: int = Field(description="总数（分页前）。")


class AgentSchemaResponse(BaseModel):
    """**Deprecated.** JSON Schema fragments used by the frontend to
    render the agent create / edit forms.

    Superseded by :class:`AgentSchemaV2Response`, which returns the full
    :class:`AgentData` JSON Schema in a single ``schema`` field so newly
    added agent fields (like the ``invite_config`` sub-model) reach the
    frontend automatically without the router having to know about them.

    The frontend previously split :class:`AgentData` into three
    hand-picked sections (``identity``, ``context_config``,
    ``react_config``) here, which required a router edit every time a
    new user-editable field landed on :class:`AgentData`. Kept for
    backwards compatibility with pre-v2 API consumers.
    """

    identity: dict = Field(
        description=(
            "Schema for the agent's identity fields (``name``, "
            "``system_prompt``)."
        ),
    )
    context_config: dict = Field(
        description="Schema for ``ContextConfig``.",
    )
    react_config: dict = Field(
        description="Schema for ``ReActConfig``.",
    )


# The ``schema`` field name below is intentional — the wire contract for
# ``GET /agent/schema/v2`` is ``{"schema": ...}`` so the response is
# self-documenting. Pydantic v2's :meth:`BaseModel.schema` is a
# deprecated legacy classmethod (superseded by ``model_json_schema``);
# a like-named instance field triggers a cosmetic "shadows an attribute
# in parent BaseModel" warning that is irrelevant here because we never
# call the legacy classmethod. Suppress it locally instead of adding an
# alias that would obscure the wire contract at every call site.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "schema" in "AgentSchemaV2Response"',
    )

    class AgentSchemaV2Response(BaseModel):
        """Response for ``GET /agent/schema/v2``.

        Wraps the full :class:`AgentData` JSON Schema in a single
        ``schema`` field so the frontend can render every user-editable
        property without the router having to enumerate them.
        """

        schema: dict = Field(
            description=(
                "Full :class:`AgentData` JSON Schema. All user-editable "
                "fields appear as top-level entries in ``properties`` — "
                "the frontend derives its section grouping from this "
                "single schema."
            ),
        )
