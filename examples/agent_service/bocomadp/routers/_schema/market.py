# -*- coding: utf-8 -*-
"""Request / response schemas for the BocomADP agent-market router."""
from datetime import datetime

from pydantic import BaseModel, Field


class MarketAgentView(BaseModel):
    """一条平台市场智能体视图（agents 行 + market 档案 + 实时热度）。"""

    id: str = Field(description="智能体 id（agents.id）。")
    name: str = Field(description="智能体名称（payload 里的 AgentData.name）。")
    source: str = Field(
        description="来源：user（自建）/ team（团长对话中派生的 worker）。",
    )
    tag: str = Field(
        description="标签（自由字符串）。平台智能体创建时自动写入默认标签"
        "（未分类），清标/下架重置回默认——恒有值，不会是 null。",
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
    """一条市场档案（PUT / DELETE /agent/market/{agent_id} 的返回体）。"""

    agent_id: str = Field(description="智能体 id。")
    tag: str = Field(description="标签（清标/下架后为默认标签“未分类”）。")
    created_at: datetime | None = Field(default=None, description="建档时间。")
    updated_at: datetime | None = Field(default=None, description="最后修改时间。")


class MarketUpsertRequest(BaseModel):
    """Request body for PUT /agent/market/{agent_id}."""

    tag: str = Field(
        default="",
        description=(
            "自由标签（≤64 字符），原样存储、不做清单校验；"
            "传空串表示重置回默认标签（未分类）。"
        ),
    )
