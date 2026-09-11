# -*- coding: utf-8 -*-
"""外部 skillhub 提供者（迁移自 ``bankcomm_adp.skills.external_hub``）。

对外部 skillhub HTTP API 的薄异步客户端，通过
:class:`~agentscope.app.hub._skill._base.SkillHubBase` 接口暴露目录与
下载能力，使 Web UI 与 workspace 流程将其视为普通 skill hub。

认证为 cookie 式、token 驱动：调用方通过 :meth:`set_token` 每次请求
传入 ``guwpToken``；每次调用都会向登录端点换取新的 ``SESSION``
cookie（无缓存）。

服务地址从环境变量 ``BOCOMADP_EXTERNAL_SKILLHUB_URL`` 读取（或 ``.env``）。
"""
from __future__ import annotations

import os
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, AsyncIterator

from agentscope._logging import logger
from agentscope.app.hub._error import HubError
from agentscope.app.hub._skill._base import SkillArchive, SkillHubBase

from ._card import SkillCard, SkillHubPage

if TYPE_CHECKING:
    import httpx

#: 目录查询端点路径（命名空间走 query 参数）。
CATALOG_PATH = "/api/web/skills"

#: 下载端点前缀 —— 最终 URL 为
#: ``{base_url}{DOWNLOAD_PREFIX}/{namespace}/{card_id}/download``。
DOWNLOAD_PREFIX = "/api/web/skills"

#: 目录命名空间。
CATALOG_NAMESPACE = "global"

#: 当前用户已上传 skill 的端点路径。
MY_SKILLS_PATH = "/api/web/me/skills"

#: 当前用户收藏的 skill 端点路径。
MY_STARS_PATH = "/api/web/me/stars"

#: 全部分类标签端点路径（树形，两级：一级类目 + children）。
LABELS_PATH = "/api/web/labels"

#: 默认流式块大小（64 KiB）。
DEFAULT_CHUNK_SIZE = 64 * 1024

#: 默认服务地址（未配置 ``BOCOMADP_EXTERNAL_SKILLHUB_URL`` 时使用）。
DEFAULT_BASE_URL = "http://53.12.9.18/skillhub-server"


def _default_skillhub_url() -> str:
    """从环境变量读取外部 skillhub 地址（兼容 ``.env``），带默认值。"""
    return os.environ.get(
        "BOCOMADP_EXTERNAL_SKILLHUB_URL",
        DEFAULT_BASE_URL,
    )


class ExternalSkillHub(SkillHubBase):
    """基于部署方自有 skillhub 服务器的 skill hub。

    .. code-block:: python

        hub = ExternalSkillHub()                 # base_url 取环境变量
        hub.set_token("guwp_...")                # 可选，按请求设置
        page = await hub.list_skills(user_id="alice", q="write")
        archive = await hub.download("alice", "write")
    """

    def __init__(
        self,
        hub_id: str = "external",
        display_name: str = "External SkillHub",
        description: str = "外部 skillhub 目录",
        icon_url: str | None = None,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """初始化外部 skillhub 提供者。

        Args:
            hub_id (`str`): 路由中寻址该 hub 的稳定 id。
            display_name (`str`): 用户可见名称。
            description (`str`): 用户可见描述。
            icon_url (`str | None`): hub 图标。
            base_url (`str | None`): skillhub 服务地址；``None`` 时
                取 ``BOCOMADP_EXTERNAL_SKILLHUB_URL``（或默认值）。
            api_token (`str | None`): 初始 ``guwpToken``，可后续通过
                :meth:`set_token` 更新。
            timeout (`float`): 单请求超时（秒）。
        """
        super().__init__(hub_id, display_name, description, icon_url)
        self.base_url = (base_url or _default_skillhub_url()).rstrip("/")
        self.timeout = timeout
        self._guwp_token = api_token
        self._client: "httpx.AsyncClient | None" = None

    def set_token(self, token: str | None) -> None:
        """更新用于 cookie 刷新的 ``guwpToken``。

        可逐请求调用——下一次调用会用新 token 重新认证。
        """
        self._guwp_token = token

    # ── 生命周期 ────────────────────────────────────────────────

    async def __aenter__(self) -> "ExternalSkillHub":
        """打开共享 HTTP 客户端。"""
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """关闭共享 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> "httpx.AsyncClient":
        """返回共享客户端；未进入上下文时按需创建。"""
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _headers(self, cookie: str) -> dict[str, str]:
        """构造请求头（含会话 cookie）。"""
        return {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0",
        }

    # ── 原样透传（不加工）────────────────────────────────────────

    async def _get_json(self, url: str) -> dict:
        """GET 一个 JSON 端点，**原样**返回响应体（不做任何字段映射/裁剪）。

        异常统一转 :class:`HubError`（与 :meth:`list_skills` 一致）。
        """
        return await self._request_json("GET", url)

    async def _request_json(self, method: str, url: str) -> dict:
        """发一个 HTTP 请求（GET / PUT / DELETE）并**原样**返回响应体。

        异常统一转 :class:`HubError`——注意这只覆盖 **HTTP 层**失败；
        远端「HTTP 200 但 ``code != 0``」的业务失败由调用方（路由）
        解析响应体后处理。

        Args:
            method (`str`): HTTP 方法，如 ``GET`` / ``PUT`` / ``DELETE``。
            url (`str`): 完整 URL。
        """
        try:
            resp = await self._http().request(
                method,
                url,
                headers=self._headers(await self._cookie()),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            status_code = getattr(getattr(e, "response", None), "status_code", 0)
            raise HubError(self.hub_id, status_code, str(e)) from e

    async def _request_text(self, method: str, url: str) -> str:
        """发一个 HTTP 请求并**原样返回文本**（远端返回纯文本，如 markdown）。

        异常统一转 :class:`HubError`（与 :meth:`_request_json` 一致）。
        """
        try:
            resp = await self._http().request(
                method,
                url,
                headers=self._headers(await self._cookie()),
            )
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            status_code = getattr(getattr(e, "response", None), "status_code", 0)
            raise HubError(self.hub_id, status_code, str(e)) from e

    @staticmethod
    def _normalize_version(version: str) -> str:
        """去掉版本号的前导 ``v``：``v20260904.074311`` → ``20260904.074311``。

        只去**第一个**字符（不能用 ``lstrip("v")``，会把连续 v 全吃掉）；
        非 v 开头原样返回。
        """
        v = (version or "").strip()
        return v[1:] if v[:1].lower() == "v" else v

    def _skill_file_url(
        self,
        namespace: str,
        slug: str,
        version: str,
        path: str,
    ) -> str:
        """技能文件内容 URL：``/api/web/skills/{ns}/{slug}/versions/{v}/file?path=``。"""
        import urllib.parse

        return (
            f"{self.base_url}{CATALOG_PATH}/"
            f"{urllib.parse.quote(namespace, safe='')}/"
            f"{urllib.parse.quote(slug, safe='')}/versions/"
            f"{urllib.parse.quote(version, safe='')}/file"
            f"?path={urllib.parse.quote(path, safe='')}"
        )

    async def get_skill_file(
        self,
        user_id: str,
        slug: str,
        version: str,
        namespace: str = CATALOG_NAMESPACE,
        path: str = "SKILL.md",
    ) -> str:
        """读取技能内某个文件的内容（默认 ``SKILL.md``），**返回纯文本原样内容**。

        远端：``GET {base}/api/web/skills/{ns}/{slug}/versions/{v}/file?path=SKILL.md``，
        返回 **text/plain 的 markdown 原文**（不是 JSON 信封）。

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
            slug (`str`): 技能 slug（目录项 ``slug``）。
            version (`str`): 版本号；远端形如 ``v20260904.074311``，
                此处会**自动去掉前导 v** 再拼 URL。
            namespace (`str`): 命名空间，默认 ``global``。
            path (`str`): 技能内文件路径，默认 ``SKILL.md``。

        Returns:
            `str`: 远端返回的原始文本（markdown 原文）。
        """
        url = self._skill_file_url(
            namespace,
            slug,
            self._normalize_version(version),
            path,
        )
        return await self._request_text("GET", url)

    def _star_url(self, skill_id: str) -> str:
        """收藏/取消收藏端点 URL（远端用**数字 id**，非 slug）。"""
        import urllib.parse

        return (
            f"{self.base_url}{CATALOG_PATH}/"
            f"{urllib.parse.quote(str(skill_id), safe='')}/star"
        )

    async def star_skill(self, user_id: str, skill_id: str) -> dict:
        """收藏一个 skill —— ``PUT /api/web/skills/{id}/star``。

        **原样返回**远端响应：成功为 ``{"code": 0, "msg": "Updated "
        "successfully", "data": null, ...}``；远端「HTTP 200 但
        ``code != 0``」的业务失败也原样返回，由调用方判定。

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
            skill_id (`str`): 远端技能的**数字 id**（非 slug）。
        """
        return await self._request_json("PUT", self._star_url(skill_id))

    async def unstar_skill(self, user_id: str, skill_id: str) -> dict:
        """取消收藏一个 skill —— ``DELETE /api/web/skills/{id}/star``。

        返回语义同 :meth:`star_skill`。
        """
        return await self._request_json("DELETE", self._star_url(skill_id))

    def _catalog_url(
        self,
        q: str | None,
        page: int,
        limit: int,
        label: str = "",
        sort: str = "",
    ) -> str:
        """拼目录查询 URL（命名空间 / 标签 / 排序走 query 参数）。

        Args:
            q (`str | None`): 关键字搜索。
            page (`int`): 页码（从 0 开始）。
            limit (`int`): 每页数量。
            label (`str`): 标签 slug（来自 ``/api/web/labels``），远端按
                标签过滤；空串为不过滤。
            sort (`str`): 排序（透传远端）；空串为远端默认。
        """
        import urllib.parse

        return (
            f"{self.base_url}{CATALOG_PATH}"
            f"?page={page}&q={urllib.parse.quote(q or '', safe='')}"
            f"&size={limit}&sort={urllib.parse.quote(sort, safe='')}"
            f"&label={urllib.parse.quote(label, safe='')}"
            f"&namespace={CATALOG_NAMESPACE}"
        )

    async def list_skills_raw(
        self,
        user_id: str,
        q: str | None = None,
        page: int = 0,
        limit: int = 20,
        label: str | None = None,
        sort: str | None = None,
    ) -> dict:
        """目录查询 —— **原样返回远端响应**，不做任何加工。

        远端结构示例（完整透传，含 ``code`` / ``msg`` / ``data`` /
        ``timestamp`` / ``requestId``）::

            {
              "code": 0,
              "msg": "Fetched successfully",
              "data": {"items": [...], "total": 308, "page": 0, "size": 1},
              "timestamp": "...",
              "requestId": "..."
            }

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
            q (`str | None`): 关键字搜索。
            page (`int`): 页码（从 0 开始）。
            limit (`int`): 每页数量。
            label (`str | None`): 标签 slug（来自 :meth:`list_labels_raw`
                返回项的 ``slug``），远端按标签过滤；``None`` 不过滤。
            sort (`str | None`): 排序（透传远端）；``None`` 用远端默认。
        """
        return await self._get_json(
            self._catalog_url(q, page, limit, label or "", sort or ""),
        )

    async def list_uploaded_skills_raw(
        self,
        user_id: str,
        page: int = 0,
        size: int = 5,
    ) -> dict:
        """我的上传查询 —— **原样返回远端响应**，不做任何加工。"""
        url = f"{self.base_url}{MY_SKILLS_PATH}?page={page}&size={size}"
        return await self._get_json(url)

    async def list_labels_raw(self, user_id: str) -> dict:
        """全部分类标签查询 —— **原样返回远端响应**，不做任何加工。

        远端：``GET {base_url}/api/web/labels``，无分页，返回信封里
        ``data`` 是**数组**（非 ``items/total`` 对象），两级树形：

        ::

            {"code": 0, "msg": "Fetched successfully",
             "data": [{"id": 46, "slug": "intelligent-development",
                       "level": 1, "parentId": 0, "type": "RECOMMENDED",
                       "displayName": "智能研发",
                       "children": [{"id": 2, "slug": "review",
                                     "level": 2, "parentId": 46, ...}]}],
             "timestamp": "...", "requestId": "..."}

        带登录态（``guwpToken``）时远端可能额外返回 ``PRIVILEGED``
        类目，故 token 由调用方按需透传，本方法不强制。

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
        """
        return await self._get_json(f"{self.base_url}{LABELS_PATH}")

    async def list_starred_skills_raw(
        self,
        user_id: str,
        page: int = 0,
        size: int = 5,
    ) -> dict:
        """我的收藏查询 —— **原样返回远端响应**，不做任何加工。

        远端：``GET {base_url}/api/web/me/stars?page=&size=``，按用户
        隔离（登录态 cookie 携带身份），返回结构与目录/我的上传一致
        （``{code, msg, data:{items, total, page, size}, ...}``）。

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
            page (`int`): 页码（从 0 开始）。
            size (`int`): 每页数量。
        """
        url = f"{self.base_url}{MY_STARS_PATH}?page={page}&size={size}"
        return await self._get_json(url)

    # ── 认证 ────────────────────────────────────────────────────

    async def _cookie(self) -> str:
        """为当前 token 返回一个新的 ``SESSION=...`` cookie。

        无缓存：每次调用都会用当前 ``guwpToken``（:meth:`set_token`
        设置）向登录端点重新认证。无 token 时返回空（匿名会话）。
        """
        token = self._guwp_token
        if not token:
            return ""

        import json

        login_url = f"{self.base_url}/api/v1/auth/third-party/login"
        body = json.dumps(
            {"loginMethod": "TOKEN", "platform": "GUWP", "token": token},
        )
        try:
            resp = await self._http().post(
                login_url,
                content=body,
                headers=self._headers(""),
            )
            resp.raise_for_status()
            new_session_id = resp.headers.get("x-session-id", "")
            if new_session_id:
                return f"SESSION={new_session_id}"
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Failed to refresh external skillhub cookie: %s",
                e,
            )
        return ""

    # ── SkillHubBase ─────────────────────────────────────────────

    async def list_skills(
        self,
        user_id: str,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        label: str | None = None,
        sort: str | None = None,
    ) -> SkillHubPage:
        """浏览目录。``cursor`` 以 ``page:N`` 编码上游页码。

        ``label``（标签 slug）与 ``sort`` 透传远端；``None`` 用默认。
        """
        page = 0
        if cursor and cursor.startswith("page:"):
            try:
                page = int(cursor.split(":", 1)[1])
            except ValueError:
                page = 0

        data = await self._get_json(
            self._catalog_url(q, page, limit, label or "", sort or ""),
        )

        payload = data.get("data") or {}
        items = payload.get("items") or []
        total = payload.get("total") or 0

        cards = [
            self._to_card(item)
            for item in items
            if item.get("slug")
        ]
        next_cursor = f"page:{page + 1}" if (page + 1) * limit < total else None
        return SkillHubPage(
            cards=cards,
            next_cursor=next_cursor,
            total=total,
        )

    def _to_card(self, item: dict) -> SkillCard:
        """由一条目录记录构造 :class:`SkillCard`。"""
        slug = item["slug"]
        return SkillCard(
            hub_id=self.hub_id,
            id=slug,
            name=slug,
            description=item.get("summary", "") or "",
            metadata={
                k: v
                for k, v in item.items()
                if k not in ("slug", "summary")
            },
        )

    async def list_uploaded_skills(
        self,
        user_id: str,
        page: int = 0,
        size: int = 5,
    ) -> SkillHubPage:
        """浏览当前用户上传到 skillhub 的 skill。

        需先通过 :meth:`set_token` 设置 ``guwpToken``——端点按用户
        隔离，会话 cookie 携带身份。``page`` / ``size`` 以查询参数
        拼接到远程 URL（``?page=..&size=..``）。

        Args:
            user_id (`str`): 用户标识。
            page (`int`): 页码，默认 0。
            size (`int`): 每页数量，默认 5。
        """
        data = await self.list_uploaded_skills_raw(user_id, page=page, size=size)

        payload = data.get("data") or {}
        items = payload.get("items") or []
        total = payload.get("total")
        if total is None:
            total = len(items)

        return SkillHubPage(
            cards=[self._to_card(item) for item in items if item.get("slug")],
            next_cursor=None,
            total=total,
        )

    async def get_skill(self, user_id: str, card_id: str) -> SkillCard:
        """尚未实现。

        远程服务目前只暴露目录与下载端点；未接入单卡详情端点，因此
        通过本 hub 的“安装进库”（``POST /hub/skill/.../install``）会
        失败，直到实现为止。

        Raises:
            NotImplementedError: 当前恒抛。
        """
        raise NotImplementedError(
            "ExternalSkillHub.get_skill is not implemented yet — the "
            "remote skillhub exposes no single-card detail endpoint.",
        )

    async def download(
        self,
        user_id: str,
        card_id: str,
        version: str | None = None,
        namespace: str = CATALOG_NAMESPACE,
    ) -> SkillArchive:
        """打开 skill 归档流
        （``{base}/api/web/skills/{namespace}/{id}/download``）。

        响应头在此处等待——缺失的 skill（404）在调用方开始安装前抛出；
        body 保持惰性，归档可被直接管道送入 workspace 而无需整体驻留内存。

        Args:
            user_id (`str`): 用户标识（透传给远端鉴权用）。
            card_id (`str`): 技能 slug。
            version (`str | None`): 版本（远端暂未使用，保留扩展位）。
            namespace (`str`): 命名空间（如 ``global``），由前端传入；
                默认 ``global``。
        """
        import urllib.parse

        url = (
            f"{self.base_url}{DOWNLOAD_PREFIX}/"
            f"{urllib.parse.quote(namespace, safe='')}/"
            f"{urllib.parse.quote(card_id, safe='')}/download"
        )
        client = self._http()
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(
                client.stream(
                    "GET",
                    url,
                    headers=self._headers(await self._cookie()),
                ),
            )
            if response.status_code == 404:
                raise KeyError(card_id)
            if response.status_code >= 400:
                body = await response.aread()
                raise HubError(
                    self.hub_id,
                    response.status_code,
                    body.decode("utf-8", errors="replace"),
                )
            return SkillArchive("zip", self._drain(stack, response))
        except Exception:
            await stack.aclose()
            raise

    async def _drain(
        self,
        stack: AsyncExitStack,
        response: Any,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> AsyncIterator[bytes]:
        """逐块产出归档字节，结束时关闭流。"""
        try:
            async for chunk in response.aiter_bytes(chunk_size):
                yield chunk
        finally:
            await stack.aclose()
