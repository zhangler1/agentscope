# -*- coding: utf-8 -*-
"""交心技能市场的 HTTP 路由。

- ``POST /skill/jiaoxin/list`` —— 分页查询技能（上游
  ``bocomListSkill.do``），返回上游完整响应信封原样字典；
- ``POST /skill/jiaoxin/content`` —— 查询技能 SKILL.md 内容（上游
  ``bocomParseSkillPackageContent.do``），原样返回；
- ``POST /skill/jiaoxin/download`` —— 下载指定技能并解压安装进
  会话工作区（上游 ``bocomExportSkill.upload``）。
"""
from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentscope.app.deps import (
    get_current_user_id,
    get_storage,
    get_workspace_manager,
)
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase

from ._jiaoxin_hub import DEFAULT_BASE_URL  # 兼容层见 _jiaoxin_hub.py
from ._jiaoxin_hub import HubError, JiaoXinSkillHub, SkillArchive

jiaoxin_skillhub_router = APIRouter(
    prefix="/skill/jiaoxin",
    tags=["jiaoxin-skillhub"],
)


async def _install_archive(workspace: Any, archive: SkillArchive, name: str) -> None:
    """把技能归档安装进工作区（双路径兼容）。

    - fork 工作区（有 ``add_skill_archive``）：归档流直通沙箱解压，
      服务端零缓冲；
    - 官方 2.0.5 ``LocalWorkspace``（只有 ``add_skill``）：先落服务端
      临时目录并解压，再按目录复制安装（临时目录用完即清）。
    """
    if hasattr(workspace, "add_skill_archive"):
        await workspace.add_skill_archive(archive.stream, archive.format, name)
        return

    with tempfile.TemporaryDirectory(prefix="jiaoxin-skill-") as tmp:
        archive_path = os.path.join(tmp, f"skill.{archive.format}")
        with open(archive_path, "wb") as f:
            async for chunk in archive.stream:
                f.write(chunk)

        extract_dir = os.path.join(tmp, "extract")
        os.makedirs(extract_dir, exist_ok=True)
        if archive.format == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(extract_dir)
        else:
            mode = "r:gz" if archive.format == "tar.gz" else "r:"
            with tarfile.open(archive_path, mode) as tf:
                # filter="data"：拒绝绝对路径/路径穿越成员（3.12+ 内建防护）
                tf.extractall(extract_dir, filter="data")

        # 定位含 SKILL.md 的目录（兼容归档根直放或包一层顶层目录）
        skill_root = extract_dir
        if not os.path.isfile(os.path.join(skill_root, "SKILL.md")):
            for entry in os.listdir(extract_dir):
                candidate = os.path.join(extract_dir, entry)
                if os.path.isfile(os.path.join(candidate, "SKILL.md")):
                    skill_root = candidate
                    break
            else:
                raise ValueError("The skill archive contains no SKILL.md")

        await workspace.add_skill(skill_root)


class ListSkillsRequest(BaseModel):
    """交心技能列表查询请求体（与上游 bocomListSkill.do 字段一一对应，全部可选）。"""

    keyword: str | None = Field(
        default=None,
        description="全局关键字模糊搜索（匹配 name/displayName/description/"
        "displayDescription）。",
    )
    name: str | None = Field(
        default=None,
        description="技能名称精确匹配。",
    )
    displayName: str | None = Field(
        default=None,
        description="展示名称。",
    )
    status: str | None = Field(
        default=None,
        description="技能状态：DRAFT / PENDING_REVIEW / PUBLISHED / "
        "REJECTED / DELISTED。",
    )
    namespace: str | None = Field(
        default=None,
        description="命名空间 slug 精确匹配（如 global）。",
    )
    labelSlugs: str | None = Field(
        default=None,
        description="标签 slug，逗号分隔，OR 语义。",
    )
    channel: str | None = Field(
        default=None,
        description="渠道 slug，逗号分隔，OR 语义。",
    )
    sort: str | None = Field(
        default=None,
        description="排序方式：recommended / downloads / latest。",
    )
    myOnly: bool | None = Field(
        default=None,
        description="是否仅查当前用户的技能。",
    )
    favoritesOnly: bool | None = Field(
        default=None,
        description="是否仅查当前用户收藏的技能。",
    )
    installedOnly: bool | None = Field(
        default=None,
        description="是否仅查已安装的技能。",
    )
    developer: str | None = Field(
        default=None,
        description="发布人用户ID。",
    )
    loginName: str | None = Field(
        default=None,
        description="发布人登录名。",
    )
    source: str | None = Field(
        default=None,
        description="来源筛选，逗号分隔，OR 语义。",
    )
    publishedOnly: bool | None = Field(
        default=None,
        description="是否仅查已发布且未下架的技能。",
    )
    page: int = Field(default=1, ge=1, description="页码。")
    size: int = Field(default=20, ge=1, le=100, description="每页条数（不超过100）。")
    startDate: str | None = Field(
        default=None,
        description="创建时间范围-起始，如 2025-01-01。",
    )
    endDate: str | None = Field(
        default=None,
        description="创建时间范围-结束。",
    )
    updateStartDate: str | None = Field(
        default=None,
        description="更新时间范围-起始。",
    )
    updateEndDate: str | None = Field(
        default=None,
        description="更新时间范围-结束。",
    )
    arena: str | None = Field(
        default=None,
        description="运营场次过滤：OFFICIAL / TEST。",
    )
    deptId: str | None = Field(
        default=None,
        description="部门ID，用于 DEPT 类型可见性 SQL 过滤。",
    )
    orders: list[dict[str, Any]] | None = Field(
        default=None,
        description='排序条件，如 [{"column":"createdAt","asc":false}]，'
        "column 支持 createdAt/updatedAt/displayName 等。",
    )


@jiaoxin_skillhub_router.post(
    "/list",
    summary="分页查询交心技能市场的技能",
    description=(
        "调用上游 bocomListSkill.do，按条件分页查询技能。返回上游完整"
        "响应信封原样字典（RSP_BODY + RSP_HEAD）；上游业务失败"
        "（TRAN_SUCCESS=0）或 HTTP 失败时同样原样透传状态码与响应体，"
        "不做包装，由调用方按 TRAN_SUCCESS 自行判读。"
    ),
)
async def list_jiaoxin_skills(
    body: ListSkillsRequest,
    guwp_token: str = Header(
        ...,
        alias="guwp-token",
        description="交心技能市场网关令牌。",
    ),
) -> Response:
    """查询交心技能市场，上游响应原样透传给调用方。"""
    # base_url 可经 JIAOXIN_SKILLHUB_BASE_URL 覆盖（本地联调指向 mock 服务）
    base_url = os.getenv("JIAOXIN_SKILLHUB_BASE_URL", DEFAULT_BASE_URL)
    async with JiaoXinSkillHub(token=guwp_token, base_url=base_url) as hub:
        try:
            # 字段名与上游 param 一致，仅过滤 None 项后透传
            payload = await hub.list_skills(
                **body.model_dump(exclude_none=True),
            )
        except HubError as e:
            # 上游 HTTP 失败：原样透传状态码与响应体，不包 502/detail
            return Response(
                status_code=e.status_code,
                content=e.message,
                media_type="application/json",
            )
    return JSONResponse(content=payload)


class SkillContentRequest(BaseModel):
    """交心技能 SKILL.md 内容查询请求体。"""

    name: str = Field(description="技能名（上游 skillName），如 mail-risk-scan。")


@jiaoxin_skillhub_router.post(
    "/content",
    summary="查询技能的 SKILL.md 内容",
    description=(
        "调用上游 bocomParseSkillPackageContent.do（REQ_MESSAGE 表单"
        "形态），返回上游完整响应信封原样字典（RSP_BODY.result 即"
        "SKILL.md 文本）；上游业务失败或 HTTP 失败时同样原样透传，"
        "不做包装。"
    ),
)
async def get_jiaoxin_skill_content(
    body: SkillContentRequest,
    guwp_token: str = Header(
        ...,
        alias="guwp-token",
        description="交心技能市场网关令牌。",
    ),
) -> Response:
    """查询技能的 SKILL.md 内容，上游响应原样透传给调用方。"""
    # base_url 可经 JIAOXIN_SKILLHUB_BASE_URL 覆盖（本地联调指向 mock 服务）
    base_url = os.getenv("JIAOXIN_SKILLHUB_BASE_URL", DEFAULT_BASE_URL)
    async with JiaoXinSkillHub(token=guwp_token, base_url=base_url) as hub:
        try:
            payload = await hub.get_skill_content(body.name)
        except HubError as e:
            # 上游 HTTP 失败：原样透传状态码与响应体，不包 502/detail
            return Response(
                status_code=e.status_code,
                content=e.message,
                media_type="application/json",
            )
    return JSONResponse(content=payload)


class DownloadSkillRequest(BaseModel):
    """交心技能下载安装请求体。"""

    agent_id: str = Field(description="目标会话所属 agent。")
    session_id: str = Field(description="目标会话。")
    name: str = Field(description="技能名，如 OCR识别。")
    namespace_slug: str = Field(default="Global", description="命名空间 slug。")
    channel: str = Field(default="aioffice", description="渠道 slug。")
    version: str | None = Field(
        default=None,
        description="技能版本；不传取上游默认（最新）。",
    )
    isCheck: bool | None = Field(
        default=None,
        description="透传给上游的 isCheck 标志；不传则不下发该字段。",
    )


@jiaoxin_skillhub_router.post(
    "/download",
    summary="下载交心技能并安装进会话工作区",
    description=(
        "调用上游 bocomExportSkill.upload 下载指定技能归档（流式），"
        "解压安装进 agent/session 对应的工作区 skills/ 目录。"
    ),
)
async def download_jiaoxin_skill(
    body: DownloadSkillRequest,
    guwp_token: str = Header(
        ...,
        alias="guwp-token",
        description="交心技能市场网关令牌。",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> dict:
    """下载指定技能并解压安装进会话工作区。"""
    # agent / session 归属校验
    if await storage.get_agent(user_id, body.agent_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent {body.agent_id!r} not found for user {user_id!r}.",
        )
    session_record = await storage.get_session(
        user_id, body.agent_id, body.session_id,
    )
    if session_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session {body.session_id!r} not found.",
        )

    workspace = await workspace_manager.get_workspace(
        user_id,
        body.agent_id,
        body.session_id,
        session_record.config.workspace_id,
    )

    # 下载（流式）并解压安装；base_url 可经 JIAOXIN_SKILLHUB_BASE_URL 指向 mock
    base_url = os.getenv("JIAOXIN_SKILLHUB_BASE_URL", DEFAULT_BASE_URL)
    async with JiaoXinSkillHub(token=guwp_token, base_url=base_url) as hub:
        try:
            archive = await hub.download(
                body.name,
                namespace_slug=body.namespace_slug,
                channel=body.channel,
                version=body.version,
                isCheck=body.isCheck,
            )
            await _install_archive(workspace, archive, body.name)
        except HubError as e:
            status = 404 if e.status_code == 404 else 502
            raise HTTPException(status_code=status, detail=e.message) from e
        except KeyError as e:
            raise HTTPException(
                status_code=404,
                detail=f"Skill {body.name!r} not found in the JiaoXin market.",
            ) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "skill": body.name,
        "version": body.version,
        "namespace_slug": body.namespace_slug,
        "channel": body.channel,
    }
