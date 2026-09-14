# -*- coding: utf-8 -*-
"""外部 skillhub 提供者（迁移自 ``bankcomm_adp.skills.external_hub``）。

对外部 skillhub HTTP API 的薄异步客户端，通过
:class:`~agentscope.app.hub._skill._base.SkillHubBase` 接口暴露目录与
下载能力，使 Web UI 与 workspace 流程将其视为普通 skill hub。

认证为 cookie 式、**本地 AES 凭证驱动**：以请求方身份 ``user_id``（即
``X-User-ID`` 头）作为 OA 账号，本地生成 AES 凭证（见
:mod:`._skillhub_auth`）后向登录端点换取 ``SESSION`` cookie。cookie
**按 OA 分键缓存**（TTL 内复用），同一 OA 的并发登录自动合流
（single-flight），因此不再需要、也不支持逐请求设置 token。

服务地址从环境变量 ``BOCOMADP_EXTERNAL_SKILLHUB_URL`` 读取（或 ``.env``）；
AES 凭证相关配置见 :mod:`._skillhub_auth`（``SKILLHUB_CHANNEL_RAND`` /
``SKILLHUB_PLATFORM``）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, AsyncIterator

from agentscope._logging import logger
from agentscope.app.hub._error import HubError
from agentscope.app.hub._skill._base import SkillArchive, SkillHubBase

from ._card import SkillCard, SkillHubPage
from ._skillhub_auth import build_auth_token, skillhub_platform

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

#: 登录端点路径（拼在 ``base_url`` 之后）。
LOGIN_PATH = "/api/v1/auth/third-party/login"

#: 会话 cookie 缓存时长（秒）。取 240s（小于 AES 凭证 5 分钟有效期）以保守
#: 复用；真正的失效信号是业务请求返回 401 —— 届时丢弃缓存并重登一次。
SESSION_TTL_SECONDS = 240.0

#: 登录失败后的冷却时长（秒）：窗口内不再打上游，避免重试风暴撞限流。
LOGIN_FAILURE_COOLDOWN_SECONDS = 5.0

#: 默认服务地址（未配置 ``BOCOMADP_EXTERNAL_SKILLHUB_URL`` 时使用）。
DEFAULT_BASE_URL = "http://53.12.9.18/skillhub-server"


def _default_skillhub_url() -> str:
    """从环境变量读取外部 skillhub 地址（兼容 ``.env``），带默认值。"""
    return os.environ.get(
        "BOCOMADP_EXTERNAL_SKILLHUB_URL",
        DEFAULT_BASE_URL,
    )


def _session_id_from_set_cookie(raw: str) -> str:
    """从 ``Set-Cookie`` 头里解析 ``SESSION=<id>``（``x-session-id`` 的兜底）。

    部分网关不返回 ``x-session-id``，而是把会话放在 ``Set-Cookie`` 里；此
    处只取会话值，不含 ``SESSION=`` 前缀。
    """
    match = re.search(
        r"(?:^|;\s*)SESSION=([^;]+)",
        raw or "",
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


class ExternalSkillHub(SkillHubBase):
    """基于部署方自有 skillhub 服务器的 skill hub。

    认证：以 ``user_id``（``X-User-ID``）为 OA 账号本地生成 AES 凭证换取
    会话 cookie；cookie 按 OA 分键缓存，并发请求之间互不影响。

    .. code-block:: python

        hub = ExternalSkillHub()                 # base_url 取环境变量
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
            timeout (`float`): 单请求超时（秒）。
        """
        super().__init__(hub_id, display_name, description, icon_url)
        self.base_url = (base_url or _default_skillhub_url()).rstrip("/")
        self.timeout = timeout
        #: OA → (``SESSION=...`` cookie, 过期时刻；``time.monotonic()``)
        self._sessions: dict[str, tuple[str, float]] = {}
        #: OA → 进行中的登录任务（并发调用共享同一任务）
        self._login_tasks: dict[str, "asyncio.Task[str]"] = {}
        #: OA → 登录失败冷却截止时刻（``time.monotonic()``）
        self._cooldowns: dict[str, float] = {}
        self._client: "httpx.AsyncClient | None" = None

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
        """构造请求头（会话 cookie 非空时才带 ``Cookie``）。"""
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    # ── 原样透传（不加工）────────────────────────────────────────

    async def _get_json(self, url: str, oa: str = "") -> dict:
        """GET 一个 JSON 端点，**原样**返回响应体（不做任何字段映射/裁剪）。

        异常统一转 :class:`HubError`（与 :meth:`list_skills` 一致）。

        Args:
            url (`str`): 完整 URL。
            oa (`str`): OA 账号（即请求的 ``user_id``），用于取会话 cookie。
        """
        return await self._request_json("GET", url, oa)

    async def _request_json(self, method: str, url: str, oa: str = "") -> dict:
        """发一个 HTTP 请求（GET / PUT / DELETE）并**原样**返回响应体。

        异常统一转 :class:`HubError`——注意这只覆盖 **HTTP 层**失败；
        远端「HTTP 200 但 ``code != 0``」的业务失败由调用方（路由）
        解析响应体后处理。

        Args:
            method (`str`): HTTP 方法，如 ``GET`` / ``PUT`` / ``DELETE``。
            url (`str`): 完整 URL。
            oa (`str`): OA 账号（即请求的 ``user_id``），用于取会话 cookie。
        """
        resp = await self._send(method, url, oa)
        return resp.json()

    async def _request_text(self, method: str, url: str, oa: str = "") -> str:
        """发一个 HTTP 请求并**原样返回文本**（远端返回纯文本，如 markdown）。

        异常统一转 :class:`HubError`（与 :meth:`_request_json` 一致）。

        Args:
            method (`str`): HTTP 方法。
            url (`str`): 完整 URL。
            oa (`str`): OA 账号（即请求的 ``user_id``），用于取会话 cookie。
        """
        resp = await self._send(method, url, oa)
        return resp.text

    async def _send(self, method: str, url: str, oa: str = "") -> Any:
        """发请求：先取该 OA 的会话 cookie，401 时重登一次并重试。

        Args:
            method (`str`): HTTP 方法。
            url (`str`): 完整 URL。
            oa (`str`): OA 账号（即请求的 ``user_id``）。

        Returns:
            `Any`: ``httpx.Response``（已 ``raise_for_status``）。

        Raises:
            HubError: 登录失败，或远端返回 4xx/5xx / 网络异常。
        """
        cookie = await self._session_cookie(oa)
        try:
            resp = await self._http().request(
                method,
                url,
                headers=self._headers(cookie),
            )
            if resp.status_code == 401 and cookie:
                # 会话已被远端失效：丢弃缓存 → 重登一次 → 重试一次
                logger.warning(
                    "External skillhub session rejected (401): oa=%s "
                    "cookie=%s -> relogin and retry once",
                    oa,
                    cookie,
                )
                self._invalidate_session(oa)
                cookie = await self._session_cookie(oa)
                resp = await self._http().request(
                    method,
                    url,
                    headers=self._headers(cookie),
                )
            resp.raise_for_status()
            return resp
        except HubError:
            raise
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
        return await self._request_text("GET", url, user_id)

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
        return await self._request_json("PUT", self._star_url(skill_id), user_id)

    async def unstar_skill(self, user_id: str, skill_id: str) -> dict:
        """取消收藏一个 skill —— ``DELETE /api/web/skills/{id}/star``。

        返回语义同 :meth:`star_skill`。
        """
        return await self._request_json(
            "DELETE",
            self._star_url(skill_id),
            user_id,
        )

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
            user_id,
        )

    async def list_uploaded_skills_raw(
        self,
        user_id: str,
        page: int = 0,
        size: int = 5,
    ) -> dict:
        """我的上传查询 —— **原样返回远端响应**，不做任何加工。"""
        url = f"{self.base_url}{MY_SKILLS_PATH}?page={page}&size={size}"
        return await self._get_json(url, user_id)

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
        return await self._get_json(f"{self.base_url}{LABELS_PATH}", user_id)

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
        return await self._get_json(url, user_id)

    # ── 认证：本地 AES 凭证 → 登录换 cookie ──────────────────────

    async def _session_cookie(self, oa: str) -> str:
        """取该 OA 的 ``SESSION=...`` cookie（缓存 / 单飞 / 失败冷却）。

        cookie 按 **OA 分键缓存**（OA 即请求的 ``user_id``），不依赖任何
        实例级可变状态，因此并发请求之间不会互相覆盖或串号：

        - 命中缓存（TTL 内）→ 直接返回，不再登录
        - 未命中 → 同一 OA 的并发调用共享同一个登录任务（single-flight）
        - 登录失败 → 进入冷却窗口并抛 :class:`HubError`（不静默降级）

        Args:
            oa (`str`): OA 账号。空值（如无身份的框架调用）返回空串，
                表示匿名会话。

        Returns:
            `str`: ``"SESSION=<id>"``；匿名时为空串。

        Raises:
            HubError: 登录失败，或该 OA 正处于失败冷却窗口内。
        """
        account = (oa or "").strip()
        if not account:
            return ""

        cached = self._sessions.get(account)
        if cached is not None:
            cookie, expires_at = cached
            if expires_at > time.monotonic():
                logger.debug(
                    "External skillhub session cache hit: oa=%s cookie=%s "
                    "remaining=%.0fs",
                    account,
                    cookie,
                    expires_at - time.monotonic(),
                )
                return cookie
            self._sessions.pop(account, None)

        cooldown_until = self._cooldowns.get(account, 0.0)
        if cooldown_until > time.monotonic():
            raise HubError(
                self.hub_id,
                0,
                f"login for {account!r} is in cooldown after a failure",
            )

        task = self._login_tasks.get(account)
        if task is None:
            task = asyncio.create_task(self._login(account))
            self._login_tasks[account] = task
            # 完成后立刻移出，避免失败的 future 永久毒化后续调用
            task.add_done_callback(
                lambda _task, key=account: self._login_tasks.pop(key, None),
            )
        # shield：等待方被取消时不牵连其他等待者共享的登录任务
        return await asyncio.shield(task)

    async def _login(self, oa: str) -> str:
        """用本地生成的 AES 凭证登录，返回 ``"SESSION=<id>"`` cookie。

        失败时记录冷却并抛 :class:`HubError`——**不**静默降级为匿名，
        否则鉴权故障会伪装成「目录里技能变少」，难以排查。

        Args:
            oa (`str`): OA 账号（非空，由 :meth:`_session_cookie` 保证）。

        Returns:
            `str`: ``"SESSION=<id>"``。

        Raises:
            HubError: 登录请求失败，或响应里没有会话标识。
        """
        body = json.dumps(
            {
                "loginMethod": "AUTH",
                "token": build_auth_token(oa),
                "platform": skillhub_platform(),
                "platForm": skillhub_platform(),
            },
        )
        login_url = f"{self.base_url}{LOGIN_PATH}"
        try:
            resp = await self._http().post(
                login_url,
                content=body,
                headers=self._headers(""),
            )
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            self._cooldowns[oa] = (
                time.monotonic() + LOGIN_FAILURE_COOLDOWN_SECONDS
            )
            logger.error(
                "External skillhub login failed (oa=%s): %s",
                oa,
                e,
            )
            raise HubError(
                self.hub_id,
                getattr(getattr(e, "response", None), "status_code", 0),
                f"login failed for oa={oa!r}: {e}",
            ) from e

        session_id = (resp.headers.get("x-session-id") or "").strip()
        if not session_id:
            # 兜底：部分网关把会话放在 ``Set-Cookie: SESSION=...``
            session_id = _session_id_from_set_cookie(
                resp.headers.get("set-cookie") or "",
            )
        if not session_id:
            self._cooldowns[oa] = (
                time.monotonic() + LOGIN_FAILURE_COOLDOWN_SECONDS
            )
            logger.error(
                "External skillhub login response has no session id (oa=%s)",
                oa,
            )
            raise HubError(
                self.hub_id,
                0,
                f"login response has no session id (oa={oa!r})",
            )

        cookie = f"SESSION={session_id}"
        self._sessions[oa] = (
            cookie,
            time.monotonic() + SESSION_TTL_SECONDS,
        )
        self._cooldowns.pop(oa, None)
        # 每次「新获取」的 cookie 都打印（含会话值，属敏感信息；如需脱敏，
        # 把 cookie 换成 f"{session_id[:8]}…" 即可）。
        logger.info(
            "External skillhub login ok: oa=%s session_id=%s cookie=%s "
            "ttl=%.0fs",
            oa,
            session_id,
            cookie,
            SESSION_TTL_SECONDS,
        )
        return cookie

    def _invalidate_session(self, oa: str) -> None:
        """丢弃某 OA 的缓存 cookie（远端返回 401 时调用）。"""
        self._sessions.pop((oa or "").strip(), None)

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
            user_id,
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

        端点按用户隔离：以 ``user_id``（OA）本地生成 AES 凭证换取会话
        cookie，cookie 按 OA 缓存复用。``page`` / ``size`` 以查询参数
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
                    headers=self._headers(
                        await self._session_cookie(user_id),
                    ),
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
