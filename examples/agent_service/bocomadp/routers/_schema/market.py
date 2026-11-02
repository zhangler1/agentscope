# -*- coding: utf-8 -*-
"""Request / response schemas for the BocomADP agent-market router."""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class MarketAgentView(BaseModel):
    """一条市场智能体视图（agents 行 + 市场名单标签 + 实时热度）。"""

    id: str = Field(description="智能体 id（agents.id）。")
    name: str = Field(description="智能体名称（payload 里的 AgentData.name）。")
    description: str = Field(
        default="",
        description=(
            "智能体简介（payload 里的 AgentData.description）；历史脏数据"
            "缺字段时兜底空串。"
        ),
    )
    system_prompt: str = Field(
        default="",
        description=(
            "智能体提示词（payload 里的 AgentData.system_prompt，创建/编辑"
            "时填的那份）；历史脏数据缺字段时兜底空串。"
        ),
    )
    source: str = Field(
        description="来源：user（自建）/ team（团长对话中派生的 worker）。",
    )
    tag: str = Field(
        description=(
            "标签（自由字符串）。空串 = 未打标——前端渲染时可用"
            "\"未分类\"做展示占位（展示文案，库里不存）。"
        ),
    )
    department: str = Field(
        default="",
        description="所属部门（发布时填写，approve 上架时随行带入）。",
    )
    system_name: str = Field(
        default="",
        description="所属系统（发布时填写）。",
    )
    description: str = Field(
        default="",
        description="说明（发布时填写的智能体用途介绍）。",
    )
    heat: int = Field(
        default=0,
        description=(
            "实时热度 = sessions 表中该智能体的会话数。列表接口同样返回，"
            "但列表默认按 updated_at 排序、不按热度。"
        ),
    )
    created_at: datetime | None = Field(default=None, description="创建时间。")
    updated_at: datetime | None = Field(default=None, description="最后更新时间。")


class MarketListResponse(BaseModel):
    """Response body for GET /agent/market."""

    agents: list[MarketAgentView] = Field(description="市场智能体列表。")
    total: int = Field(description="筛选后的总数（分页前）。")


class MarketEntryView(BaseModel):
    """一条市场名单记录（发布 / 打标接口的返回体）。"""

    agent_id: str = Field(description="智能体 id。")
    tag: str = Field(description="标签；空串 = 未打标。")
    created_at: datetime | None = Field(default=None, description="上架时间。")
    updated_at: datetime | None = Field(default=None, description="最后修改时间。")


class MarketUpsertRequest(BaseModel):
    """Request body for PUT /agent/market/{agent_id}."""

    tag: str = Field(
        default="",
        description=(
            "自由标签（≤64 字符），原样存储、不做清单校验；"
            "传空串表示撕标（tag 置空，回到未打标状态）。"
        ),
    )


# ---------------------------------------------------------------------------
# 发布审批（agent_market_review）
# ---------------------------------------------------------------------------


class MarketPublishRequest(BaseModel):
    """Request body for POST /agent/market/{agent_id}/publish（发布弹窗表单）。

    部门 / 系统 / 业务条线 / 说明**全部必填**（弹窗带 * 号）；业务条线
    是下拉单选，选项清单由前端模板维护，后端只校验非空、不做枚举
    硬校验（自由字符串存）。
    """

    @field_validator("tag")
    @classmethod
    def _tag_not_blank(cls, v: str) -> str:
        """业务条线防手滑：纯空白（如只敲了空格）也视为未填。"""
        if not v.strip():
            raise ValueError("业务条线（tag）必填，不能为空。")
        return v.strip()

    department: str = Field(
        min_length=1,
        max_length=64,
        description="所属部门（必填，≤64 字符），如：网络金融部。",
    )
    system_name: str = Field(
        min_length=1,
        max_length=64,
        description="所属系统（必填，≤64 字符），如：智能体平台。",
    )
    tag: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "业务条线（**必填**，≤64 字符）：合规审计/会计审计/智能研发/"
            "公司金融/智慧办公/营运支撑/风险防控等，下拉单选；"
            "后端只校验非空（含防纯空白），不做枚举硬校验。"
            "即市场标签 tag——发布时选择，上架后可用打标/撕标接口修改。"
        ),
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description="说明（必填，≤500 字符）：智能体用途介绍。",
    )


class MarketPublishStatusView(BaseModel):
    """发布/审批状态视图。

    ``publish`` / ``approve`` / ``reject`` 三个
    接口统一返回这套字段；``status`` 取值：

    - ``not_submitted``：从未提交过发布（合成状态，表里无记录）；
    - ``pending``：审核中；
    - ``approved``：已通过（= 已在市场名单内）；
    - ``rejected``：已拒绝（``reason`` 带拒绝理由）。
    """

    agent_id: str = Field(description="智能体 id。")
    status: str = Field(
        description=(
            "发布状态：not_submitted / pending / approved / rejected。"
        ),
    )
    applicant: str = Field(
        default="",
        description="申请人（owner user_id）；not_submitted 时为空串。",
    )
    reason: str = Field(
        default="",
        description=(
            "审批结论文案（同一字段按状态区分含义）：rejected = 驳回"
            "理由；approved = 审批意见（选填，可为空串）；pending 恒"
            "为空串。"
        ),
    )
    reviewer: str = Field(
        default="",
        description="最近一次审批人；pending / not_submitted 时为空串。",
    )
    department: str = Field(
        default="",
        description="所属部门（发布弹窗填写）；not_submitted 时为空串。",
    )
    system_name: str = Field(
        default="",
        description="所属系统（发布弹窗填写）。",
    )
    tag: str = Field(
        default="",
        description="业务条线（发布时必填；即市场标签）。",
    )
    description: str = Field(
        default="",
        description="说明（发布弹窗填写的用途介绍）。",
    )
    reviewed_at: datetime | None = Field(
        default=None,
        description="最近一次审批时间；未审批过为 null。",
    )
    created_at: datetime | None = Field(
        default=None,
        description="申请提交时间；not_submitted 时为 null。",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="最近状态变更时间；not_submitted 时为 null。",
    )


class MarketReviewItemView(BaseModel):
    """审批列表条目（审批记录 + 从 agents 表现查的智能体内容）。

    ``name`` / ``system_prompt`` **不存**审批表，每次查询从 agents
    表现取——审批人看到的永远是智能体当前最新内容。
    """

    agent_id: str = Field(description="智能体 id。")
    name: str = Field(description="智能体名称（agents.payload 实时取）。")
    system_prompt: str = Field(
        default="",
        description="智能体提示词（agents.payload 实时取）。",
    )
    applicant: str = Field(description="申请人（owner user_id）。")
    status: str = Field(description="审批状态：pending/approved/rejected。")
    reason: str = Field(
        default="",
        description=(
            "审批结论文案（同一字段按状态区分含义）：rejected = 驳回"
            "理由；approved = 审批意见；pending 恒为空串。"
        ),
    )
    reviewer: str = Field(
        default="",
        description="最近一次审批人；待办条目恒为空串。",
    )
    department: str = Field(
        default="",
        description="所属部门（发布弹窗填写，审批人查看）。",
    )
    system_name: str = Field(
        default="",
        description="所属系统（发布弹窗填写，审批人查看）。",
    )
    tag: str = Field(
        default="",
        description="业务条线（发布时必填，即市场标签）。",
    )
    description: str = Field(
        default="",
        description="说明（发布弹窗填写的用途介绍，审批人查看）。",
    )
    reviewed_at: datetime | None = Field(
        default=None,
        description="审批时间；待办条目恒为 null。",
    )
    created_at: datetime | None = Field(default=None, description="申请时间。")
    updated_at: datetime | None = Field(
        default=None,
        description="最近状态变更时间。",
    )


class MarketReviewListResponse(BaseModel):
    """Response body for GET /agent/market/reviews."""

    reviews: list[MarketReviewItemView] = Field(description="审批记录列表。")
    total: int = Field(description="该状态下的总数（分页前）。")
    status_counts: dict = Field(
        default_factory=dict,
        description=(
            "各状态计数（全量，不随 status/keyword 筛选变化）："
            '{"pending": n, "approved": n, "rejected": n}——审核页顶部'
            "统计卡（待审核/已通过/已驳回/全部申请）数据源，全部申请"
            " = 三者相加。"
        ),
    )


class MarketApproveRequest(BaseModel):
    """Request body for POST /agent/market/reviews/{agent_id}/approve."""

    reason: str = Field(
        default="",
        max_length=200,
        description=(
            "审批意见（**选填**，≤200 字符）。通过时可不填或写审批"
            "说明；落库到 reason 字段（approved 状态下 reason 存的是"
            "审批意见而非拒绝理由，前端按状态区分文案）。"
        ),
    )


class MarketRejectRequest(BaseModel):
    """Request body for POST /agent/market/reviews/{agent_id}/reject."""

    reason: str = Field(
        min_length=1,
        max_length=200,
        description="拒绝理由（必填，1~200 字符），申请人可见。",
    )


# ---------------------------------------------------------------------------
# 审批人白名单（JSON 文件 + 接口管理）
# ---------------------------------------------------------------------------


class MarketReviewerView(BaseModel):
    """一条生效审批人。"""

    user_id: str = Field(description="审批人 user_id。")


class MarketReviewersResponse(BaseModel):
    """Response body for GET/POST/PUT /agent/market/reviewers。"""

    reviewers: list[MarketReviewerView] = Field(
        description="生效审批人名单（JSON 文件内容，排序返回；可能为空）。",
    )


class MarketReviewersUpdateRequest(BaseModel):
    """Request body for POST/PUT /agent/market/reviewers."""

    user_ids: list[str] = Field(
        default_factory=list,
        description=(
            "审批人 user_id 清单。POST = 批量新增（幂等）；"
            "PUT = 全量覆盖（不可为空，末位保护 409）。"
        ),
    )
