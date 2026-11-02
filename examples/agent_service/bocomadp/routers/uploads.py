# -*- coding: utf-8 -*-
"""文件上传 / 列举 / 下载 / 删除路由（沙箱感知重构版）。

上传文件统一经 ``workspace.get_backend()`` 落盘到 ``{workdir}/user-data/uploads``，
本模块提供独立的 ``GET /files/upload`` 端点用于下载已上传的原始文件或转换后的
``.md``（与通用文件下载 ``GET /files/{path}`` 互补：前者按 user/session/文件名
从元数据定位，后者按 workdir 相对路径直读）。


路径策略完全对齐 ``routers/workspace_files.py``：

- 通过 ``StorageBase.get_session`` 读取会话持久化的 ``workspace_id``；
- ``WorkspaceManagerBase.get_workspace`` 解析出沙箱内的 ``workspace.workdir``；
- 上传目录 = ``{workdir}/user-data/uploads``，由 ``UploadProvider`` 经
  ``workspace.get_backend()`` 落盘——双 PVC（session PVC）与共享 PVC
  （``/workspace/sessions/{id}``）天然保证 session 间隔离；
- 本地模式（``LocalWorkspaceManager``）走同一套 ``backend.write_file``，
  落盘到宿主机 workdir（同样 session 隔离），无需分支特判。

``.md`` 转换在 host 侧用第三方库完成（与沙箱无关），转换文本随原始文件
一并写入工作区，并存入 ``UploadedFile.markdown`` 供中间件渲染。
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, status, UploadFile
from pydantic import BaseModel, Field

from agentscope.app.deps import get_current_user_id, get_storage, get_workspace_manager
from agentscope.app.storage import StorageBase
from agentscope.app.workspace_manager import WorkspaceManagerBase

from bocomadp.config.uploads_config import get_upload_config
from bocomadp.uploads import file_conversion

logger = logging.getLogger(__name__)
from bocomadp.uploads.db import UploadedFile, UploadedFileCreate, get_uploads_db
from bocomadp.uploads.manager import (
    FileSizeExceeded,
    PathTraversalError,
    TooManyFiles,
    UploadError,
    detect_image_mime,
    encode_image_base64,
    image_ext_to_mime,
    is_image,
    normalize_filename,
    to_upload_rel_path,
    to_virtual_path,
    validate_path_traversal,
)

uploads_router = APIRouter(prefix="/files", tags=["upload"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class UploadListResponse(BaseModel):
    files: list[UploadedFile]
    count: int


class UploadLimits(BaseModel):
    """上传能力/限制信息（供前端展示与阈值判断）。"""

    max_file_size_mb: float
    max_files_per_session: int
    streaming_threshold_mb: float


# ---------------------------------------------------------------------------
# 解析 workspace（与 workspace_files.py 同源）
# ---------------------------------------------------------------------------
async def _resolve_workspace(
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
):
    """按会话记录解析其绑定的 workspace（含 PER_SESSION 语义）。"""
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


def _raise_upload_error(err: Exception) -> None:
    if isinstance(err, PathTraversalError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))
    if isinstance(err, FileSizeExceeded | TooManyFiles):
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(err))
    if isinstance(err, UploadError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(err))
    raise err


def _upload_abs(backend, workdir: str, name: str) -> str:
    return backend.join_path(workdir, to_upload_rel_path(name))


async def _dedupe_session_filename(
    *,
    backend,
    workdir: str,
    user_id: str,
    session_id: str,
    stored_name: str,
) -> str:
    """同会话同名文件自动改名（``A.docx`` → ``A(1).docx`` → ``A(2).docx``）。

    同会话内重传同名文件时若直接覆盖磁盘并复用路径，先前轮次消息引用的
    ``virtual_path`` 会静默指向新内容（元数据旧记录与磁盘真身脱节）。
    改名让每次上传获得独立路径：物理文件分开，元数据不再出现同三键的
    多条记录，逐轮历史还原精确到当轮版本。跨会话同名由 workdir 物理
    隔离，无需去重。占用判定同时看元数据库与磁盘，防止单边残留。

    Raises:
        `UploadError`: 序号耗尽（极端情况，防御死循环）。
    """
    db = get_uploads_db()

    async def _taken(name: str) -> bool:
        if db.get_by_session_file(user_id, session_id, name):
            return True
        return await backend.file_exists(_upload_abs(backend, workdir, name))

    if not await _taken(stored_name):
        return stored_name

    stem, ext = os.path.splitext(stored_name)
    for seq in range(1, 1000):
        candidate = f"{stem}({seq}){ext}"
        if not await _taken(candidate):
            return candidate
    raise UploadError(
        f"too many same-name uploads for {stored_name!r} in session {session_id!r}",
    )


async def _persist_uploaded_bytes(
    *,
    user_id: str,
    agent_id: str,
    session_id: str,
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
    original_name: str,
    data: bytes,
    content_type: str | None,
    stored_name_override: str | None = None,
    auto_parse: bool = True,
) -> UploadedFile:
    """落盘原始文件 +（可选）内容加工 + 写入 uploads DB。

    ``POST /files/upload`` 端点与 ``context.custom_params.additional_urls`` 下载
    共用本函数，保证两条路径的文件元数据与落盘格式完全一致（下游
    ``view_image_tool`` / ``<context name="files">``
    均以 uploads DB 记录为感知通道）。

    Args:
        auto_parse: 落盘后是否自动加工内容——图片转 base64 固化、文档转 .md。
            为 ``False`` 时仅落盘 + 写库，不做任何内容加工（解析交由调用方
            独立完成，例如自定义 skill），避免大文件同步转换拖慢请求。

    Raises:
        `UploadError`: 文件名非法 / 落盘失败（调用方按需处理）。
    """
    # 安全文件名（stored_name_override 供调用方指定落盘名：URL 下载时
    # 扩展名与调用方 file_name 冲突，落盘名跟随 URL 保证名与内容一致）
    stored_name = normalize_filename(
        stored_name_override or original_name or "file",
    )
    validate_path_traversal(stored_name)

    # 解析工作区与 backend（沙箱 / 本地统一）
    workspace = await _resolve_workspace(
        user_id, agent_id, session_id, storage, workspace_manager,
    )
    backend = workspace.get_backend()
    workdir = workspace.workdir

    # 同会话同名自动改名（A.docx → A(1).docx）：每次上传独立路径，
    # 先前轮次消息引用的 virtual_path 不被后续重传覆盖。
    stored_name = await _dedupe_session_filename(
        backend=backend,
        workdir=workdir,
        user_id=user_id,
        session_id=session_id,
        stored_name=stored_name,
    )
    virtual_path = to_virtual_path(stored_name)

    # 上传目录由来宾工作区的 _ensure_workspace_layout() 保证存在
    #（user-data/uploads）；backend.write_file 也会自动创建父目录，
    # 无需重复 mkdir。
    abs_target = _upload_abs(backend, workdir, stored_name)

    # 落盘原始文件
    try:
        await backend.write_file(abs_target, data)
    except Exception as e:  # noqa: BLE001
        raise UploadError(f"write failed: {e}") from e

    # 内容加工（host 侧第三方库）：图片 base64 固化 / 文档转 .md，并写回工作区。
    # auto_parse=False 时整段跳过——仅落盘 + 写库，解析交由调用方（如自定义
    # skill）按自己的节奏完成，避免大文件同步转换阻塞请求、触发网关超时。
    converted = False
    convert_format = None
    convert_error = None
    markdown = None
    base64 = None
    mime_type = None
    is_img = is_image(content_type, stored_name)
    try:
        if auto_parse and is_img:
            # 图片：直接固化为 base64 存入元数据，供 view_image_tool
            # 直接解析（不生成 .md，不内联进消息正文）。MIME 以文件头
            # 实测为准（内容优先），实测不到时按扩展名兜底。
            mime_type = detect_image_mime(data) or image_ext_to_mime(stored_name)
            if mime_type is None:
                convert_error = (
                    "unsupported image format; supported: jpg, jpeg, png, webp"
                )
            else:
                base64 = encode_image_base64(data)
        elif auto_parse and file_conversion.is_supported_format(stored_name):
            fmt, md_text = file_conversion.convert_file_bytes(
                stored_name, data, content_type,
            )
            if md_text:
                converted = True
                convert_format = fmt
                markdown = md_text
                md_name = f"{os.path.splitext(stored_name)[0]}.md"
                await backend.write_file(
                    _upload_abs(backend, workdir, md_name),
                    md_text.encode("utf-8"),
                )
    except Exception as e:  # noqa: BLE001
        convert_error = str(e)

    return get_uploads_db().add(
        UploadedFileCreate(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            workspace_id=getattr(workspace, "workspace_id", None),
            # 落盘名可被 stored_name_override 改写（扩展名冲突时跟随 URL），
            # 此处必须记调用方的原始名，UploadsMiddleware 据此在提示词中
            # 说明「原始名 vs 实际文件」的对应关系。
            original_name=original_name or stored_name,
            stored_name=stored_name,
            virtual_path=virtual_path,
            size_bytes=len(data),
            content_type=content_type,
            converted=converted,
            convert_format=convert_format,
            convert_error=convert_error,
            markdown=markdown,
            base64=base64,
            mime_type=mime_type,
        ),
    )


# ---------------------------------------------------------------------------
# URL 下载保存（deerflow context.custom_params.additional_urls）
# ---------------------------------------------------------------------------
# 对齐 deer-flow uploads.py 的下载参数：整体 60s 超时（连接 10s）+ 跟随
# 重定向；单文件大小沿用本服务 uploads 配置（max_file_size_mb）。
_URL_DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _filename_from_url(url: str) -> str:
    """从 URL 路径提取文件名（URL 解码）；无有效路径段回退 ``downloaded``。"""
    parsed = urlparse(url)
    path = parsed.path or ""
    # 目录路径（以 / 结尾）没有文件名，回退 downloaded
    name = (
        unquote(Path(path).name)
        if path and not path.endswith("/")
        else ""
    )
    return name if name and name not in {".", ".."} else "downloaded"


def _name_ext(name: str) -> str:
    """取文件名扩展名（小写带点）；无扩展名返回空串。"""
    n = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    return ("." + n.rsplit(".", 1)[-1].lower()) if "." in n else ""


def _name_ext_conflict(original_name: str, url_name: str) -> bool:
    """调用方 ``file_name`` 与 URL 推断名扩展名不一致（如 .xls vs .txt）。

    源平台可能把文档导出为文本/其他格式后存储，URL 指向的名字才是与
    内容一致的名字；此时落盘名跟随 URL，``file_name`` 仅作 original_name
    记录，由 UploadsMiddleware 在提示词中说明对应关系。
    """
    if not original_name or not url_name or url_name == "downloaded":
        return False
    o_ext = _name_ext(original_name)
    u_ext = _name_ext(url_name)
    return bool(o_ext and u_ext and o_ext != u_ext)


# ---------------------------------------------------------------------------
# 工具层依赖注入（run 任务内无 FastAPI Depends，工具须经本入口复用
# download_urls_to_session 下载文件到会话 uploads 目录）
# ---------------------------------------------------------------------------
_upload_deps_storage: StorageBase | None = None
_upload_deps_workspace_manager: WorkspaceManagerBase | None = None


def set_upload_dependencies(
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
) -> None:
    """注册全局 storage / workspace_manager（main.py 启动时调用一次）。"""
    global _upload_deps_storage, _upload_deps_workspace_manager
    _upload_deps_storage = storage
    _upload_deps_workspace_manager = workspace_manager


def get_upload_dependencies() -> tuple[StorageBase, WorkspaceManagerBase]:
    """读取已注册的 ``(storage, workspace_manager)``。

    Raises:
        RuntimeError: 尚未注册（main.py 启动序列未执行 / 测试环境未注入）。
    """
    if _upload_deps_storage is None or _upload_deps_workspace_manager is None:
        raise RuntimeError(
            "upload dependencies not registered; call "
            "set_upload_dependencies(storage, workspace_manager) "
            "at startup (main.py)"
        )
    return _upload_deps_storage, _upload_deps_workspace_manager


async def download_urls_to_session(
    user_id: str,
    agent_id: str,
    session_id: str,
    items: list[str | tuple[str, str]],
    storage: StorageBase,
    workspace_manager: WorkspaceManagerBase,
) -> list[UploadedFile]:
    """下载 URL 文件并保存到会话 uploads 目录（``context.custom_params.additional_urls``）。

    对齐 deer-flow ``download_urls_to_thread`` 语义：单个 URL 失败仅告警
    跳过（部分成功可接受），不阻断调用方流程；保存逻辑与
    ``POST /files/upload`` 完全一致（经 :func:`_persist_uploaded_bytes`
    落盘 + 图片 base64 固化 / 文档 .md 转换 + uploads DB 记录），保证
    下游链路立即可见。

    Args:
        items: 待下载的 OSS / HTTP(S) 地址列表。元素可为：

            - 裸 URL 字符串（``"http://oss/a.png"``），文件名从 URL 推断；
            - ``(url, original_name)`` 元组，``original_name`` 为空时回退
              到 URL 推断（保留前端传入的 ``file_name``）。

            同名文件经 ``_dedupe_session_filename`` 自动改名，互不覆盖。

    Returns:
        `list[UploadedFile]`: 成功保存的上传记录（按 URL 顺序）。
    """
    cfg = get_upload_config()
    if not cfg.enabled or not items:
        return []
    db = get_uploads_db()
    saved: list[UploadedFile] = []
    async with httpx.AsyncClient(
        timeout=_URL_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for entry in items:
            if isinstance(entry, tuple):
                url, original_name = entry
            else:
                url, original_name = entry, ""
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(
                    "additional_urls: URL %s returned HTTP %s",
                    url,
                    e.response.status_code,
                )
                continue
            except httpx.RequestError as e:
                logger.error(
                    "additional_urls: failed to download %s: %s",
                    url,
                    e,
                )
                continue

            content = resp.content
            if len(content) > cfg.max_file_size_bytes:
                logger.warning(
                    "additional_urls: file from %s exceeds %.1f MB limit",
                    url,
                    cfg.max_file_size_mb,
                )
                continue
            if (
                db.count_by_session(user_id, agent_id, session_id)
                >= cfg.max_files_per_session
            ):
                logger.warning(
                    "additional_urls: session %s exceeds %d files; "
                    "remaining URLs skipped",
                    session_id,
                    cfg.max_files_per_session,
                )
                break
            try:
                url_name = _filename_from_url(url)
                record = await _persist_uploaded_bytes(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    storage=storage,
                    workspace_manager=workspace_manager,
                    # 裸 URL 传入（无调用方名）时回退 URL 推断名；
                    # 扩展名冲突时（如 file_name=.xls 而 URL 实际是 .txt）
                    # 落盘名跟随 URL，保证文件名与内容一致；file_name 仅作
                    # original_name 记录，由中间件在提示词中说明对应关系。
                    original_name=original_name or url_name,
                    stored_name_override=(
                        url_name
                        if _name_ext_conflict(original_name, url_name)
                        else None
                    ),
                    data=content,
                    content_type=resp.headers.get("content-type"),
                )
            except UploadError as e:
                logger.warning(
                    "additional_urls: skip %s: %s",
                    url,
                    e,
                )
                continue
            except Exception as e:  # noqa: BLE001 —— 保存失败不阻断其余 URL
                logger.error(
                    "additional_urls: failed to save %s: %s",
                    url,
                    e,
                )
                continue
            saved.append(record)
            logger.info(
                "additional_urls: downloaded %s -> %s (%d bytes)",
                url,
                record.virtual_path,
                len(content),
            )
    return saved


# ---------------------------------------------------------------------------
# GET /files/limits — 上传能力/限制信息
# ---------------------------------------------------------------------------
@uploads_router.get("/limits", response_model=UploadLimits)
async def get_upload_limits() -> UploadLimits:
    cfg = get_upload_config()
    return UploadLimits(
        max_file_size_mb=cfg.max_file_size_mb,
        max_files_per_session=cfg.max_files_per_session,
        streaming_threshold_mb=cfg.streaming_threshold_mb,
    )


# ---------------------------------------------------------------------------
# GET /files/uploads — 列举某会话已上传文件
# ---------------------------------------------------------------------------
@uploads_router.get("/uploads", response_model=UploadListResponse)
async def list_uploads(
    agent_id: str,
    session_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> UploadListResponse:
    db = get_uploads_db()
    files = db.list_by_session(user_id, agent_id, session_id)
    return UploadListResponse(files=files, count=len(files))


# ---------------------------------------------------------------------------
# POST /files/upload — 批量上传（同一字段名 file 可携带多个文件；沙箱 / 本地 统一走 backend）
# ---------------------------------------------------------------------------
@uploads_router.post("/upload", response_model=list[UploadedFile])
async def upload_file(
    agent_id: str = Form(...),
    session_id: str = Form(...),
    # 字段名保持 ``file`` 不变：前端（uploads.ts 的 fd.append('file', ...)）
    # 与历史 curl 均以此名发多值，FastAPI 按参数名绑定表单字段，
    # 同名多值自动收成列表，前端零改动即可批量上传。
    file: list[UploadFile] = File(...),
    # 是否在落盘后自动解析内容：默认 true 保持既有行为（图片 base64 固化 /
    # 文档转 .md）；false 为纯上传，仅落盘 + 写库，解析交由调用方独立完成
    # （如自定义 skill）。大文件传 false 可避免同步转换拖慢请求、触发网关超时。
    auto_parse: bool = Form(True),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
) -> list[UploadedFile]:
    cfg = get_upload_config()
    if not cfg.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="upload disabled")
    if not file:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no file provided")

    # 限制：单会话文件数（按"已有数 + 本次批量数"一次性校验，避免逐个插时超量）
    db = get_uploads_db()
    existing = db.count_by_session(user_id, agent_id, session_id)

    results: list[UploadedFile] = []
    for upload in file:
        # 限制：单文件大小
        data = await upload.read()
        if len(data) > cfg.max_file_size_bytes:
            raise FileSizeExceeded(
                f"file {upload.filename!r} {len(data)//1024//1024}MB exceeds "
                f"limit {cfg.max_file_size_mb}MB",
            )
        if existing + len(results) >= cfg.max_files_per_session:
            raise TooManyFiles(
                f"session {session_id!r} exceeds {cfg.max_files_per_session} files",
            )

        # 落盘 + 转换 + DB 记录（与 context.custom_params.additional_urls 下载共用
        # _persist_uploaded_bytes，保证两条路径行为一致）
        try:
            record = await _persist_uploaded_bytes(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                storage=storage,
                workspace_manager=workspace_manager,
                original_name=upload.filename or "file",
                data=data,
                content_type=upload.content_type,
                auto_parse=auto_parse,
            )
        except UploadError as e:
            _raise_upload_error(e)
            raise  # pragma: no cover —— _raise_upload_error 必然抛 HTTPException
        results.append(record)

    return results


# ---------------------------------------------------------------------------
# GET /files/upload/download — 下载已上传文件（支持原始文件或 .md）
#   特用独立路径，避免与通用文件下载 GET /files/{path} 冲突。
# ---------------------------------------------------------------------------
@uploads_router.get("/upload/download")
async def download_upload(
    agent_id: str,
    session_id: str,
    filename: str,
    md: bool = False,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
):
    validate_path_traversal(filename)
    db = get_uploads_db()
    stored = db.get_by_session_file(user_id, session_id, filename, agent_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="upload not found")

    workspace = await _resolve_workspace(
        user_id, agent_id, session_id, storage, workspace_manager,
    )
    backend = workspace.get_backend()
    workdir = workspace.workdir

    name = f"{os.path.splitext(filename)[0]}.md" if md else filename
    abs_path = _upload_abs(backend, workdir, name)
    try:
        raw = await backend.read_file(abs_path)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"read failed: {e}")

    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    encoded = quote(name)
    headers = {"Content-Disposition": f'attachment; filename*=UTF-8\'\'{encoded}'}
    return Response(content=raw, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# DELETE /files/upload — 删除已上传文件
# ---------------------------------------------------------------------------
@uploads_router.delete("/upload", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload(
    agent_id: str,
    session_id: str,
    filename: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    workspace_manager: WorkspaceManagerBase = Depends(get_workspace_manager),
):
    validate_path_traversal(filename)
    db = get_uploads_db()
    stored = db.get_by_session_file(user_id, session_id, filename, agent_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="upload not found")

    workspace = await _resolve_workspace(
        user_id, agent_id, session_id, storage, workspace_manager,
    )
    backend = workspace.get_backend()
    workdir = workspace.workdir

    for nm in (filename, f"{os.path.splitext(filename)[0]}.md"):
        abs_path = _upload_abs(backend, workdir, nm)
        try:
            if await backend.file_exists(abs_path):
                await backend.remove_file(abs_path)
        except Exception:  # noqa: BLE001
            pass

    db.delete(user_id, agent_id, session_id, stored.stored_name)
