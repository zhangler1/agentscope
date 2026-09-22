# -*- coding: utf-8 -*-
"""外部 skill hub 相关路由（迁移自 ``bankcomm_adp.routers.skill_router``）。

三个端点全部支持任意隔离策略（含 PER_SESSION）：workspace 一律通过
会话记录（DB 中持久化的 ``config.workspace_id``）解析，Bubblewrap 等
沙箱后端下精确指向对应会话的工作目录。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from agentscope._logging import logger
from agentscope.app._service import ResourceAccessService
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_skill_hubs,
    get_storage,
    get_workspace_manager,
)
from agentscope.app.hub import SkillHubBase
from agentscope.app.hub._error import HubError
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase
from agentscope.workspace._base import (
    DEFAULT_MAX_EXTRACTED_BYTES,
    _EXTRACT_ARCHIVE_SHIM,
)
from agentscope.workspace._utils import DEFAULT_SKILLS_DIR

from ..skills._oa_context import OA_HEADER, get_current_oa
from ..skills._schema import AgentSkillsListResponse, SkillActionResponse, SkillInfo
from ..skills._skillhub_auth import (
    OA_SEPARATOR,
    build_auth_token,
    skillhub_platform,
)

skill_router = APIRouter(prefix="/workspace", tags=["skill-external"])

#: AES 凭证的有效期（秒）。上游登录接口按 5 分钟判定，且凭证明文含时间戳，
#: 因此必须「用前现造」，不可缓存（见 ``skills/_skillhub_auth`` 模块说明）。
AES_TOKEN_TTL_SECONDS = 300


def _raise_remote_error(payload: dict) -> None:
    """远端业务失败（HTTP 2xx 但 ``code != 0``）——取远端 ``code``/``msg`` 抛 400。

    远端用 ``code`` 表达业务结果（``0`` 成功），可能以 **HTTP 200 +
    ``code != 0``** 返回失败（如未登录、技能不存在），因此不能只看
    HTTP 状态码，必须解析 ``code``。
    """
    code = payload.get("code")
    if code in (0, "0", None):  # 0 / "0" = 成功；无 code 字段视为非信封结构，跳过
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": code,
            "msg": payload.get("msg") or "",
            "timestamp": payload.get("timestamp"),
            "requestId": payload.get("requestId"),
        },
    )


def _hub_error_to_502(e: HubError) -> HTTPException:
    """上游失败（非 2xx / 网络错误）→ 统一 502，detail 带远端真实状态码。"""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"Remote skillhub error (hub={e.hub_id}, "
            f"status={e.status_code}): {e.message}"
        ),
    )


async def _resolve_workspace(
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
):
    """按会话记录解析其绑定的 workspace（含 PER_SESSION 语义）。

    从 DB 读取会话持久化的 ``config.workspace_id``，而非现算——
    沙箱后端据此定位对应会话的工作目录。
    """
    session_record = await storage.get_session(user_id, agent_id, session_id)
    if session_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id!r} not found.",
        )
    return await workspace_manager.get_workspace(
        user_id,
        agent_id,
        session_id,
        session_record.config.workspace_id,
    )


def _external_hub(
    hubs: dict[str, SkillHubBase],
) -> SkillHubBase:
    """取注册的外部 skillhub，未注册则 404。"""
    hub = hubs.get("external") if hubs else None
    if hub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No 'external' skill hub is registered.",
        )
    return hub


def _set_token(hub: SkillHubBase, guwp_token: str | None) -> None:
    """逐请求刷新 hub 的 guwpToken（仅 ExternalSkillHub 有该方法）。"""
    set_token = getattr(hub, "set_token", None)
    if set_token is not None:
        set_token(guwp_token)


def _card_version(metadata: dict) -> str:
    """取远端目录项里的版本号。

    只认**已发布版本** ``publishedVersion.version``（形如 ``v20260907.102346``）；
    远端该项为 ``null``（未发布）时返回空串，不回退到 ``headlineVersion``。
    ``external_hub._to_card()`` 把除 ``slug``/``summary`` 外的字段原样放进
    ``card.metadata``，所以这里直接读。
    """
    node = metadata.get("publishedVersion")
    if isinstance(node, dict) and node.get("version"):
        return str(node["version"])
    return ""


async def _session_used_names(
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
) -> set[str]:
    """返回会话 workspace 中已装备的 skill 名集合。

    session 无效 → 404；其他解析失败 → 降级为空集（不拖垮查询）。
    """
    try:
        workspace = await _resolve_workspace(
            user_id,
            agent_id,
            session_id,
            storage,
            workspace_manager,
        )
        agent_skills = await workspace.list_skills()
        return {s.name for s in agent_skills}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to resolve workspace for agent %s, marking no "
            "skills as used: %s",
            agent_id,
            e,
        )
        return set()


@skill_router.get(
    "/skillhub/aes-token",
    summary="Get SkillHub AES credential",
    description=(
        "Locally generate the SkillHub third-party login credential "
        "(identical algorithm to the frontend ``lib/skillhub/token.ts``):\n"
        "``key = <Beijing date YYYYMMDD>(8) + <SKILLHUB_CHANNEL_RAND>(8)``,"
        " ``token = hex(AES-128-ECB/PKCS7(\"{oa}#{13-digit ms timestamp}\"))``.\n"
        "``oa`` defaults to the ``oa`` header, then ``X-User-ID``. The "
        "credential is "
        "only valid for ~5 minutes, so it is built on **every** call — "
        "never cache it."
    ),
)
async def get_skillhub_aes_token(
    oa: str | None = Query(
        default=None,
        description="OA 账号；省略时依次取 oa 请求头 / X-User-ID",
    ),
    oa_header: str | None = Header(default=None, alias=OA_HEADER),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """本地生成 SkillHub 登录用的 AES 凭证（明文只含 OA 与毫秒时间戳）。

    - ``oa`` 省略时依次取 ``oa`` 请求头 → ``X-User-ID``；为空 → 400，
      含分隔符 ``#`` → 422
    - 密钥后 8 字节取 ``SKILLHUB_CHANNEL_RAND``（必须 8 个 ASCII 字符）；
      长度不为 16 字节 → 500（配置错误，非调用方问题）
    - 返回 ``{oa, token, platform, timestamp, expires_in}``：
      ``token`` 直接作为登录请求体的 ``token``，``platform`` 即登录请求体的
      ``platform`` 取值（``SKILLHUB_PLATFORM``），``timestamp`` 为明文里用到
      的毫秒时间戳，``expires_in`` 为有效期秒数（仅供调用方判断是否过期）

    ``token`` 含登录态语义、有效期约 5 分钟：**每次调用现造**，调用方拿到后
    应立即使用，不要落库/缓存。
    """
    account = (oa or oa_header or user_id).strip()
    if not account:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An OA account is required: pass ?oa= or X-User-ID.",
        )
    if OA_SEPARATOR in account:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OA account must not contain {OA_SEPARATOR!r}.",
        )

    # 密钥里的日期与明文里的时间戳共用同一个 ms 时间戳，避免跨零点不一致。
    timestamp_ms = int(time.time() * 1000)
    try:
        token = build_auth_token(account, timestamp_ms=timestamp_ms)
    except ValueError as e:
        # OA 已在上面校验，此处只可能是密钥长度问题（channel rand 配置错）
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"SkillHub AES credential misconfigured: {e}",
        ) from e

    return {
        "oa": account,
        "token": token,
        "platform": skillhub_platform(),
        "timestamp": timestamp_ms,
        "expires_in": AES_TOKEN_TTL_SECONDS,
    }


@skill_router.get(
    "/skills/external",
    summary="Get Agent Skills",
    description=(
        "Query the external skillhub catalog. Two response modes:\n"
        "- With BOTH ``agent_id`` and ``session_id``: legacy processed "
        "shape ``{skills: [{name, category, description, version, used}], "
        "total}`` — ``category`` is the remote ``namespace``, "
        "``version`` is the catalog item's **published** version (empty "
        "string when the remote reports ``publishedVersion: null``), ``used`` "
        "marks skills already equipped in the session's workspace.\n"
        "- Otherwise: the remote response verbatim "
        "(``{code, msg, data:{items, total, page, size}, timestamp, "
        "requestId}``)."
    ),
)
async def get_agent_skills(
    agent_id: str | None = Query(default=None),
    session_id: str = Query(default=""),
    page: int = Query(default=0, ge=0),
    q: str = Query(default=""),
    size: int = Query(default=10, ge=1, le=200),
    sort: str = Query(default=""),
    label: str = Query(
        default="",
        description="标签 slug（来自 /skills/labels），透传远端按标签过滤",
    ),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """查询外部 skillhub 目录，两种返回模式。

    - ``agent_id`` 与 ``session_id`` **同时提供** → 旧格式
      ``{skills: [{name, category, description, version, used}], total}``：
      ``category`` 取远端 ``namespace``，``version`` 取目录项的
      ``publishedVersion.version``（远端为 ``null`` 时给空串），
      ``used`` 标记会话 workspace 已装备的技能（session 无效 → 404）。
    - 任一缺失 → 远端响应**原样透传**（``{code, msg, data, ...}``）。

    ``agent_id`` 传了才做归属校验（不属于调用者则 404）。
    ``q`` 为关键字；``label`` 为标签 slug（远端过滤）；``sort`` 透传。
    """
    # 归属校验（仅当传了 agent_id）：不属于调用者则 404。
    if agent_id:
        await access.resolve_agent(user_id, agent_id)

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    if agent_id and session_id:
        # ── 旧格式：加工 + used 标记（category 取远端 namespace）──
        used_names = await _session_used_names(
            user_id,
            agent_id,
            session_id,
            storage,
            workspace_manager,
        )
        cursor = f"page:{page}" if page else None
        try:
            page_result = await hub.list_skills(
                oa,
                q=q or None,
                cursor=cursor,
                limit=size,
                label=label or None,
                sort=sort or None,
            )
        except HubError as e:
            raise _hub_error_to_502(e) from e

        skills_list = [
            SkillInfo(
                name=card.name,
                category=card.metadata.get("namespace") or "public",
                description=card.description or "",
                version=_card_version(card.metadata),
                used=card.name in used_names,
            )
            for card in page_result.cards
        ]
        return AgentSkillsListResponse(
            skills=skills_list,
            total=(
                page_result.total
                if page_result.total is not None
                else len(skills_list)
            ),
        ).model_dump()

    # ── 原样透传 ──
    list_raw = getattr(hub, "list_skills_raw", None)
    if list_raw is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "The 'external' skill hub does not support raw catalog "
                "query (list_skills_raw)."
            ),
        )

    # q = 关键字搜索；label = 标签 slug（来自 /skills/labels），透传远端
    # 按标签过滤；sort 透传远端。三者互不干扰。
    try:
        return await list_raw(
            oa,
            q=q or None,
            page=page,
            limit=size,
            label=label or None,
            sort=sort or None,
        )
    except HubError as e:
        raise _hub_error_to_502(e) from e


@skill_router.get(
    "/skills/bocom",
    response_model=AgentSkillsListResponse,
    summary="Get Bocom Skills",
    description=(
        "Query the Bocom skillhub catalog and return it to the "
        "frontend with the same shape as the external skillhub, "
        "marking as ``used`` the skills already present in the "
        "session's workspace (``agent_id`` and ``session_id`` are "
        "passed as query parameters)."
    ),
)
async def get_bocom_skills(
    agent_id: str = Query(...),
    session_id: str = Query(...),
    keyword: str = Query(default=""),
    status: str = Query(default="PUBLISHED"),
    namespace: str = Query(default="global"),
    labelSlugs: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    myOnly: bool = Query(default=False),
    size: int = Query(default=10, ge=1, le=200),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> AgentSkillsListResponse:
    """查询 Bocom skillhub 目录并返回（返回格式与 external hub 一致）。

    参数与 Bocom 上游 curl 请求对齐：``keyword`` / ``status`` /
    ``namespace`` / ``labelSlugs`` / ``page`` / ``myOnly`` / ``size``；
    ``agent_id`` / ``session_id`` 用于解析 workspace 以标记 ``used`` 状态。
    """
    # 归属校验：agent 必须属于（或被共享给）调用者，否则 404。
    await access.resolve_agent(user_id, agent_id)

    used_names = await _session_used_names(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )

    hub = hubs.get("bocom") if hubs else None
    if hub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No 'bocom' skill hub is registered.",
        )

    # 透传 guwp-token（逐请求设置，与 external hub 的 set_token 一致）。
    set_token = getattr(hub, "set_token", None)
    if set_token is not None:
        set_token(guwp_token)

    page_result = await hub.list_skills(
        oa,
        keyword=keyword,
        status=status,
        namespace=namespace,
        labelSlugs=labelSlugs,
        page=page,
        myOnly=myOnly,
        size=size,
    )

    skills_list = [
        SkillInfo(
            name=card.name,
            category=card.tags[0] if card.tags else "global",
            description=card.description or "",
            used=card.name in used_names,
        )
        for card in page_result.cards
    ]
    return AgentSkillsListResponse(
        skills=skills_list,
        total=(
            page_result.total
            if page_result.total is not None
            else len(skills_list)
        ),
    )


@skill_router.get(
    "/skills/uploaded",
    summary="Get Uploaded Skills",
    description=(
        "Query the external skillhub for the skills the caller uploaded. "
        "Two response modes:\n"
        "- With BOTH ``agent_id`` and ``session_id``: legacy processed "
        "shape ``{skills: [{name, category, description, version, used}], "
        "total}`` (``category`` is the remote ``namespace``, ``version`` "
        "the published version, empty when ``publishedVersion`` is null).\n"
        "- Otherwise: the remote response verbatim. ``guwpToken`` is "
        "required in both modes (user-scoped endpoint)."
    ),
)
async def get_uploaded_skills(
    agent_id: str | None = Query(default=None),
    session_id: str = Query(default=""),
    page: int = Query(default=0, ge=0),
    size: int = Query(default=5, ge=1, le=200),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """返回调用者上传到外部 skillhub 的 skill，两种返回模式。

    - ``agent_id`` 与 ``session_id`` **同时提供** → 旧格式
      ``{skills: [{name, category, description, version, used}], total}``。
    - 任一缺失 → 远端响应**原样透传**。

    ``guwpToken`` 两种模式都**必填**（用户级端点）。
    ``agent_id`` 传了才做归属校验。
    """
    # 归属校验（仅当传了 agent_id）：不属于调用者则 404。
    if agent_id:
        await access.resolve_agent(user_id, agent_id)

    if not guwp_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="guwpToken header is required for uploaded skills.",
        )

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    if agent_id and session_id:
        # ── 旧格式：加工 + used 标记（category 取远端 namespace）──
        used_names = await _session_used_names(
            user_id,
            agent_id,
            session_id,
            storage,
            workspace_manager,
        )
        try:
            page_result = await hub.list_uploaded_skills(
                oa,
                page=page,
                size=size,
            )
        except HubError as e:
            raise _hub_error_to_502(e) from e

        skills_list = [
            SkillInfo(
                name=card.name,
                category=card.metadata.get("namespace") or "public",
                description=card.description or "",
                version=_card_version(card.metadata),
                used=card.name in used_names,
            )
            for card in page_result.cards
        ]
        return AgentSkillsListResponse(
            skills=skills_list,
            total=(
                page_result.total
                if page_result.total is not None
                else len(skills_list)
            ),
        ).model_dump()

    # ── 原样透传 ──
    list_raw = getattr(hub, "list_uploaded_skills_raw", None)
    if list_raw is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "The 'external' skill hub does not support raw uploaded "
                "query (list_uploaded_skills_raw)."
            ),
        )
    try:
        return await list_raw(oa, page=page, size=size)
    except HubError as e:
        raise _hub_error_to_502(e) from e


@skill_router.get(
    "/skills/labels",
    summary="Get Skill Labels",
    description=(
        "Query all skill category labels from the remote skillhub "
        "(``GET {base}/api/web/labels``) and return the remote "
        "response **verbatim**. ``data`` is a two-level tree array "
        "(``level`` 1 categories with nested ``children``; ``type`` is "
        "``RECOMMENDED`` / ``PRIVILEGED``). ``guwpToken`` is optional "
        "— passing it may additionally surface ``PRIVILEGED`` labels."
    ),
)
async def get_skill_labels(
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """返回外部 skillhub 的全部分类标签，**原样返回远端响应**。

    - 无分页（全量返回），``data`` 为树形数组
    - ``guwpToken`` **可选**：带登录态可能额外返回 ``PRIVILEGED`` 类目
    - 业务失败（``code != 0``）→ 400 + 远端 ``code``/``msg``
    - 网络 / HTTP 失败 → 502
    """
    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    list_raw = getattr(hub, "list_labels_raw", None)
    if list_raw is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "The 'external' skill hub does not support raw labels "
                "query (list_labels_raw)."
            ),
        )
    try:
        return await list_raw(oa)
    except HubError as e:
        raise _hub_error_to_502(e) from e


@skill_router.get(
    "/skills/starred",
    summary="Get Starred Skills",
    description=(
        "Query the external skillhub for the skills the caller starred "
        "and return the remote response **verbatim** "
        "(``{code, msg, data:{items, total, page, size}, ...}``). "
        "``guwpToken`` is required (user-scoped); ``agent_id`` is "
        "optional — when given, its ownership is verified."
    ),
)
async def get_starred_skills(
    agent_id: str | None = Query(default=None),
    page: int = Query(default=0, ge=0),
    size: int = Query(default=5, ge=1, le=200),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """返回调用者在外部 skillhub 收藏的 skill，**原样返回远端响应**。

    收藏是「用户级」数据，与具体智能体无关，因此：
    - ``agent_id`` **非必填**；传了才做归属校验（必须属于或被共享给
      调用者，否则 404）
    - ``guwpToken`` **必填**——端点按用户隔离，由其派生的会话 cookie
      携带远程身份

    支持 ``page`` / ``size`` 分页（透传给远端 ``?page=&size=``）。
    """
    # 归属校验（仅当传了 agent_id 时）：不属于调用者则 404。
    if agent_id:
        await access.resolve_agent(user_id, agent_id)

    if not guwp_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="guwpToken header is required for starred skills.",
        )

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    list_raw = getattr(hub, "list_starred_skills_raw", None)
    if list_raw is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "The 'external' skill hub does not support raw starred "
                "query (list_starred_skills_raw)."
            ),
        )
    try:
        return await list_raw(oa, page=page, size=size)
    except HubError as e:
        raise _hub_error_to_502(e) from e


@skill_router.get(
    "/skill/markdown",
    summary="Get Skill Markdown",
    description=(
        "Fetch a skill's file content (default ``SKILL.md``) from the "
        "remote skillhub: ``GET {base}/api/web/skills/{namespace}/"
        "{slug}/versions/{version}/file?path=SKILL.md``. The remote "
        "returns **plain-text markdown**, wrapped here as "
        "``{\"content\": \"...\"}``. ``slug`` and ``version`` are "
        "required; a leading ``v`` in ``version`` is stripped "
        "automatically."
    ),
)
async def get_skill_markdown(
    slug: str = Query(..., description="技能 slug（目录项 slug）"),
    version: str = Query(
        ...,
        description="版本号（如 v20260904.074311，前导 v 会自动去除）",
    ),
    namespace: str = Query(default="global", description="命名空间"),
    path: str = Query(default="SKILL.md", description="技能内文件路径"),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """读取技能的 ``SKILL.md``（或指定文件）内容。

    - ``slug`` / ``version`` **必填**，缺一 → 422
    - ``namespace`` 默认 ``global``；``path`` 默认 ``SKILL.md``
    - ``guwpToken`` **可选**：公共 skill 可匿名读，私有 skill 需携带
    - 成功返回 ``{"content": "<markdown 原文>"}``
    - 远端以 JSON 信封返回业务失败（``code != 0``）→ 400 + 远端 code/msg
    - 网络 / HTTP 失败 → 502
    """
    if not slug.strip() or not version.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Both 'slug' and 'version' are required.",
        )

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    get_file = getattr(hub, "get_skill_file", None)
    if get_file is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "The 'external' skill hub does not support reading "
                "skill files (get_skill_file)."
            ),
        )

    try:
        content = await get_file(
            oa,
            slug=slug,
            version=version,
            namespace=namespace,
            path=path,
        )
    except HubError as e:
        raise _hub_error_to_502(e) from e

    # 远端正常情况下返回纯文本 markdown；若改为 JSON 信封（含 code），
    # 则走统一的业务错误映射并把信封原样透传。
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and "code" in payload:
        _raise_remote_error(payload)
        return payload

    return {"content": content}


@skill_router.put(
    "/skill/star/{skill_id}",
    summary="Star a Skill",
    description=(
        "Star (favorite) a skill on the remote skillhub: "
        "``PUT {base}/api/web/skills/{id}/star``. ``skill_id`` is the "
        "remote **numeric id** (from ``items[].id``), not the slug. "
        "``guwpToken`` is required. The remote response is returned "
        "verbatim; a business failure (HTTP 200 with ``code != 0``) is "
        "surfaced as 400 with the remote ``code``/``msg``."
    ),
)
async def star_skill(
    skill_id: str,
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """收藏一个 skill（用户级行为，与智能体无关）。

    - ``skill_id``：远端技能的**数字 id**（目录透传后的 ``items[].id``）
    - ``guwpToken``：**必填**（缺 → 400），用于换取会话 cookie
    - 成功：原样返回 ``{"code": 0, "msg": "Updated successfully", ...}``
    - 业务失败：``code != 0`` → 400 + 远端 ``code``/``msg``
    - 网络/HTTP 失败：502
    """
    if not guwp_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="guwpToken header is required for starring a skill.",
        )

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    star = getattr(hub, "star_skill", None)
    if star is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The 'external' skill hub does not support starring.",
        )

    try:
        result = await star(oa, skill_id)
    except HubError as e:
        raise _hub_error_to_502(e) from e

    _raise_remote_error(result)
    logger.info(
        "skill star: user=%s skill_id=%s result_code=%s",
        user_id,
        skill_id,
        result.get("code"),
    )
    return result


@skill_router.delete(
    "/skill/star/{skill_id}",
    summary="Unstar a Skill",
    description=(
        "Unstar a skill on the remote skillhub: "
        "``DELETE {base}/api/web/skills/{id}/star``. Same contract as "
        "the starring endpoint."
    ),
)
async def unstar_skill(
    skill_id: str,
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> dict:
    """取消收藏一个 skill（语义同 :func:`star_skill`）。"""
    if not guwp_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="guwpToken header is required for unstarring a skill.",
        )

    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)

    unstar = getattr(hub, "unstar_skill", None)
    if unstar is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The 'external' skill hub does not support unstarring.",
        )

    try:
        result = await unstar(oa, skill_id)
    except HubError as e:
        raise _hub_error_to_502(e) from e

    _raise_remote_error(result)
    logger.info(
        "skill unstar: user=%s skill_id=%s result_code=%s",
        user_id,
        skill_id,
        result.get("code"),
    )
    return result


@skill_router.post(
    "/skill/download/{skill_full_name}",
    response_model=SkillActionResponse,
    summary="Enable Skill for Agent",
    description=(
        "Download a skill from the remote skillhub into the session's "
        "workspace. ``skill_full_name`` follows ``namespace:name`` "
        "(e.g. ``global:rollback-check-sql``) — the namespace is "
        "required and is used in the remote download URL "
        "``/api/web/skills/{namespace}/{name}/download``. ``agent_id`` "
        "and ``session_id`` are passed as query parameters."
    ),
)
async def enable_agent_skill(
    skill_full_name: str,
    agent_id: str = Query(...),
    session_id: str = Query(...),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> SkillActionResponse:
    """为指定 agent 启用（下载安装）一个 skill。

    ``skill_full_name`` 遵循 ``namespace:name`` 约定（如
    ``global:rollback-check-sql``）——**namespace 必传**（即目录项的
    ``namespace`` 字段），远端下载 URL 为
    ``/api/web/skills/{namespace}/{name}/download``。已装备时幂等返回。
    目标 workspace 从持久化会话记录解析，任意隔离策略下都精确。
    """
    await access.resolve_agent(user_id, agent_id)

    if ":" not in skill_full_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "skill_full_name must be in 'namespace:name' form, "
                "e.g. 'global:rollback-check-sql'."
            ),
        )
    category, skill_name = skill_full_name.split(":", 1)

    # PER_SESSION 下 workspace id 是每会话随机值，必须来自数据库而非现算。
    workspace = await _resolve_workspace(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )

    # 已装备 —— 幂等成功。按 agent-facing 名或目录名匹配（frontmatter
    # 名可能与 slug 不同）。
    existing = await workspace.list_skills()
    if skill_name in {s.name for s in existing} or any(
        os.path.basename(s.dir.rstrip("/\\")) == skill_name
        for s in existing
    ):
        logger.info(
            "Skill '%s' already equipped in agent %s's workspace",
            skill_full_name,
            agent_id,
        )
        return SkillActionResponse(
            success=True,
            action="enabled",
            skill_id=skill_full_name,
        )

    # 经 hub 抽象下载 —— 归档流式送入 workspace 后端（沙箱兼容）。
    # namespace 由前端经 skill_full_name 的前缀传入，透传远端。
    hub = _external_hub(hubs)
    _set_token(hub, guwp_token)
    try:
        archive = await hub.download(oa, skill_name, namespace=category)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_name}' not found on the remote skillhub.",
        ) from None
    try:
        await workspace.add_skill_archive(
            archive.stream,
            archive.format,
            skill_name,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to install skill '{skill_name}': {e}",
        ) from e

    # 校验确有新 skill 落盘（解压目录可能按 frontmatter 命名）。
    refreshed = await workspace.list_skills()
    new_names = {s.name for s in refreshed} - {s.name for s in existing}
    if not new_names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Downloaded skill '{skill_name}' has no valid SKILL.md "
                "(requires 'name' and 'description' fields)."
            ),
        )

    logger.info("Enabled skill '%s' for agent '%s'", skill_full_name, agent_id)
    return SkillActionResponse(
        success=True,
        action="enabled",
        skill_id=skill_full_name,
    )


@skill_router.post(
    "/skill/download/bocom/{skill_name}",
    response_model=SkillActionResponse,
    summary="Enable Bocom Skill for Agent",
    description=(
        "Download a skill from the Bocom skillhub into the session's "
        "workspace. ``skill_name`` is the Bocom skill name (e.g. "
        "``excel智能分析``); ``agent_id`` and ``session_id`` are passed "
        "as query parameters."
    ),
)
async def enable_bocom_skill(
    skill_name: str,
    agent_id: str = Query(...),
    session_id: str = Query(...),
    namespaceSlug: str = Query(default="Global"),
    guwp_token: str | None = Header(default=None, alias="guwpToken"),
    oa: str = Depends(get_current_oa),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    access: ResourceAccessService = Depends(get_resource_access_service),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> SkillActionResponse:
    """为指定 agent 启用（下载安装）一个 Bocom skill。

    ``skill_name`` 为 Bocom 技能名（如 ``excel智能分析``），直接从
    ``bocom`` hub 下载归档并安装进会话 workspace。已装备时幂等返回。
    目标 workspace 从持久化会话记录解析，任意隔离策略下都精确。
    """
    await access.resolve_agent(user_id, agent_id)

    # PER_SESSION 下 workspace id 是每会话随机值，必须来自数据库而非现算。
    workspace = await _resolve_workspace(
        user_id,
        agent_id,
        session_id,
        storage,
        workspace_manager,
    )

    # 已装备 —— 幂等成功。按 agent-facing 名或目录名匹配（frontmatter
    # 名可能与 slug 不同）。
    existing = await workspace.list_skills()
    if skill_name in {s.name for s in existing} or any(
        os.path.basename(s.dir.rstrip("/\\")) == skill_name
        for s in existing
    ):
        logger.info(
            "Bocom skill '%s' already equipped in agent %s's workspace",
            skill_name,
            agent_id,
        )
        return SkillActionResponse(
            success=True,
            action="enabled",
            skill_id=skill_name,
        )

    # 从 bocom hub 下载 —— 归档流式送入 workspace 后端（沙箱兼容）。
    hub = hubs.get("bocom") if hubs else None
    if hub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No 'bocom' skill hub is registered.",
        )
    _set_token(hub, guwp_token)
    try:
        archive = await hub.download(
            oa,
            name=skill_name,
            namespaceSlug=namespaceSlug,
        )
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill '{skill_name}' not found on the Bocom skillhub.",
        ) from None
    try:
        await workspace.add_skill_archive(
            archive.stream,
            archive.format,
            skill_name,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Failed to install skill '{skill_name}': {e}",
        ) from e

    # 校验确有新 skill 落盘（解压目录可能按 frontmatter 命名）。
    refreshed = await workspace.list_skills()
    new_names = {s.name for s in refreshed} - {s.name for s in existing}
    if not new_names:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Downloaded skill '{skill_name}' has no valid SKILL.md "
                "(requires 'name' and 'description' fields)."
            ),
        )

    logger.info("Enabled Bocom skill '%s' for agent '%s'", skill_name, agent_id)
    return SkillActionResponse(
        success=True,
        action="enabled",
        skill_id=skill_name,
    )


# ---------------------------------------------------------------------------
# 智能体复制：技能搬运（供 POST /agent/{agent_id}/copy 调用）
# ---------------------------------------------------------------------------
# 沙箱往返（Pod exec）是这条链路最贵的部分，因此采用「整包一次搬运」：
#   源端 1 次 `tar czf - ...`（字节直接从 stdout 取，与框架 read_file 同机制）
#   目标端 1 次 `write_stream` + 1 次解包 shim = 共 3 次往返，与技能数量无关。
# 解包复用框架自己的 `_EXTRACT_ARCHIVE_SHIM`：路径穿越 / 软链名校验、
# 解压总量上限（DEFAULT_MAX_EXTRACTED_BYTES）、解完自删临时文件。


async def _aiter_bytes(blob: bytes) -> AsyncIterator[bytes]:
    """把整块字节包成 ``AsyncIterator[bytes]``（``write_stream`` 的入参）。"""
    yield blob


def _unwrap_workspace(workspace: Any) -> Any:
    """逐层解开 workspace 代理，拿到真实 workspace 对象。

    ``WhitelistWorkspaceManager`` 返回的 ``_WhitelistWorkspaceProxy``
    **继承自 ``WorkspaceBase``**：它的 ``_skills_dir`` 会命中基类属性
    （``{workdir}/skills``），根本不会走 ``__getattr__`` 代理——直接读
    代理上的 ``_skills_dir`` 会拿到**非共享**路径，导致共享 PVC 模式下
    打包一个不存在的目录（实测报错：
    ``tar: /workspace/sessions/<sid>/skills: Cannot open``）。
    """
    seen: set[int] = set()
    obj = workspace
    while True:
        inner = getattr(obj, "_workspace", None)
        if inner is None or inner is obj or id(inner) in seen:
            return obj
        seen.add(id(obj))
        obj = inner


def _skills_dir_from_skills(skills: list[Any]) -> str:
    """从 ``list_skills()`` 结果推导技能根目录。

    这是最可靠的来源：目录就是列技能时实际扫描的那一个（与代理层无关）。
    """
    for skill in skills:
        path = str(getattr(skill, "dir", "") or "").rstrip("/\\")
        if path:
            return os.path.dirname(path)
    return ""


def _skills_dir_of(workspace: Any) -> str:
    """取 workspace 的 ``skills/`` 目录绝对路径。

    共享 PVC 模式下框架把该目录覆盖为 ``/workspace/shared/skills``
    （agent 级 PVC，各会话共享），因此不能简单拼 ``{workdir}/skills``：
    先解开代理读框架的 ``_skills_dir``，取不到再按默认布局兜底。
    """
    real = _unwrap_workspace(workspace)
    override = getattr(real, "_skills_dir", None)
    if override:
        return str(override)
    return real.get_backend().join_path(
        real.workdir,
        DEFAULT_SKILLS_DIR,
    )


async def _workspace_for_agent(
    user_id: str,
    agent_id: str,
    workspace_manager: WorkspaceManagerBase,
) -> Any:
    """取某智能体的 workspace 句柄（**不要求**该智能体已有会话）。

    共享 PVC 模式下技能位于 agent 级目录，``session_id`` 只影响 workdir
    子目录，所以合成一个 session id 即可命中同一份技能目录；PER_AGENT
    模式下 workspace id 本身由 (user, agent) 决定，同样稳定。
    """
    session_id = f"skillcopy-{uuid4().hex[:12]}"
    workspace_id = workspace_manager.assign_workspace_id(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    return await workspace_manager.get_workspace(
        user_id,
        agent_id,
        session_id,
        workspace_id,
    )


async def copy_agent_skills(
    user_id: str,
    src_agent_id: str,
    dst_agent_id: str,
    workspace_manager: WorkspaceManagerBase,
) -> tuple[list[str], list[str]]:
    """把源智能体 workspace 的 ``skills/`` 整体复制到目标智能体。

    流程（方案 B —— 3 次沙箱往返，与技能数无关）：

    1. 源端 ``tar czf - -C <src_skills_dir> --exclude=.skills .``，字节取自
       ``ExecResult.stdout``（二进制安全，框架 ``read_file`` 同机制）；
    2. 目标端 ``write_stream(tmp, blob)``；
    3. 目标端用 ``_EXTRACT_ARCHIVE_SHIM`` 解包到 ``<dst_skills_dir>``。

    **同名技能直接覆盖**（不做去重、不加后缀）：调用方是"复制到新建的
    智能体"，其 ``skills/`` 通常是空的；重复复制时以源为准。

    Args:
        user_id (`str`):
            调用者（workspace 归属）。
        src_agent_id (`str`):
            源智能体 id。
        dst_agent_id (`str`):
            目标智能体 id。
        workspace_manager (`WorkspaceManagerBase`):
            工作区管理器（本地 / K8s 沙箱都由它出句柄）。

    Returns:
        `tuple[list[str], list[str]]`:
            ``(复制的技能名列表, 告警列表)``。源无技能 → ``([], [])``，
            且**不会**为目标智能体创建沙箱；任何一步失败只记告警、
            不抛异常（调用方据此降级，不影响智能体本体的复制）。
    """
    try:
        src_ws = await _workspace_for_agent(
            user_id,
            src_agent_id,
            workspace_manager,
        )
        skills = await src_ws.list_skills()
    except Exception as exc:  # noqa: BLE001 —— 读源失败只降级
        logger.warning(
            "skill copy: resolve source workspace failed (agent=%s)",
            src_agent_id,
            exc_info=True,
        )
        return [], [f"skills: 读取源技能失败: {exc}"]

    if not skills:
        # 源没有技能 → 直接结束，避免白白拉起目标沙箱（Pod 冷启动很贵）。
        logger.info(
            "skill copy: source agent %s has no skills; nothing to do",
            src_agent_id,
        )
        return [], []

    names = [s.name for s in skills]
    try:
        src_backend = src_ws.get_backend()
        # 源端优先用"列技能时实际扫描到的目录"，其次才读（解开代理后的）
        # ``_skills_dir``；两者都拿不到时退化为默认布局。
        src_dir = _skills_dir_from_skills(skills) or _skills_dir_of(src_ws)
        tar_result = await src_backend.exec_shell(
            ["tar", "czf", "-", "-C", src_dir, "--exclude=.skills", "."],
        )
        if not tar_result.ok():
            raise RuntimeError(
                "tar failed: "
                + tar_result.stderr.decode("utf-8", "replace")[:200],
            )
        blob = tar_result.stdout
        if not blob:
            raise RuntimeError("source skills archive is empty")

        dst_ws = await _workspace_for_agent(
            user_id,
            dst_agent_id,
            workspace_manager,
        )
        dst_backend = dst_ws.get_backend()
        dst_dir = _skills_dir_of(dst_ws)
        tmp = f"/tmp/.cp-skills-{uuid4().hex}.tar.gz"
        await dst_backend.write_stream(tmp, _aiter_bytes(blob))
        extract_result = await dst_backend.exec_shell(
            [
                "python3",
                "-c",
                _EXTRACT_ARCHIVE_SHIM,
                tmp,
                dst_dir,
                "tar.gz",
                str(DEFAULT_MAX_EXTRACTED_BYTES),
            ],
        )
        if not extract_result.ok():
            raise RuntimeError(
                "extract failed: "
                + extract_result.stderr.decode("utf-8", "replace")[:200],
            )
    except Exception as exc:  # noqa: BLE001 —— 搬运失败只降级
        logger.warning(
            "skill copy: %s → %s failed",
            src_agent_id,
            dst_agent_id,
            exc_info=True,
        )
        return [], [f"skills: 复制失败: {exc}"]

    logger.info(
        "skill copy: %d skill(s) copied %s → %s (%s)",
        len(names),
        src_agent_id,
        dst_agent_id,
        ", ".join(names),
    )
    return names, []


__all__ = ["skill_router", "copy_agent_skills"]
