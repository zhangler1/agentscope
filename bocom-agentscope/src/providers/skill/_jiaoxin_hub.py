# -*- coding: utf-8 -*-
"""行内交心（JiaoXin）技能市场下载 hub。

对接交心技能市场的 ``bocomExportSkill.upload`` 端点，把技能压缩包
以流式方式取回，复用 SDK 的 :class:`~agentscope.app.hub.SkillArchive`
契约（格式 + 字节流），与 ``workspace.add_skill_archive`` 无缝衔接。

.. note:: 只实现下载（``download``）：交心技能市场端点按技能名导出，
    无目录浏览/检索能力，故不实现 ``SkillHubBase`` 的 list/get 抽象方法。

.. note:: 假设上游成功响应是二进制压缩包（zip / tar / tar.gz），
    归档格式按 ``Content-Type`` 与 ``Content-Disposition`` 文件名推断，
    推断不出时默认 ``zip``；若上游返回 JSON 包装（错误或 base64 载荷），
    本实现按错误处理并原样抛出响应体——首次联调时如发现 JSON 包装
    成功响应，需要在此处补充解包逻辑。
"""
import json
import re
from contextlib import AsyncExitStack
from typing import Any, AsyncIterator, NamedTuple

try:
    # SDK 带 hub 体系（本地 fork 源码 / agentscope >= 2.0.7）
    from agentscope.app.hub import HubBase, HubError, SkillArchive
except ImportError:
    # 官方 agentscope 2.0.5 无 agentscope.app.hub —— 本地兜底定义，
    # 结构与 SDK 侧一致（HubError(hub_id, status_code, message) /
    # SkillArchive(format, stream)），保证本包在纯 2.0.5 环境可用。

    class HubError(Exception):
        """Raised when a hub's upstream registry returns a failure."""

        def __init__(self, hub_id: str, status_code: int, message: str) -> None:
            self.hub_id = hub_id
            self.status_code = status_code
            self.message = message
            super().__init__(f"{hub_id} returned {status_code}: {message}")

    class HubBase:
        """The hub identity shared by hub implementations."""

        def __init__(
            self,
            hub_id: str,
            display_name: str,
            description: str = "",
            icon_url: str | None = None,
        ) -> None:
            self.hub_id = hub_id
            self.display_name = display_name
            self.description = description
            self.icon_url = icon_url

        async def __aenter__(self) -> "HubBase":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class SkillArchive(NamedTuple):
        """A skill archive as the hub serves it."""

        format: str  # "zip" | "tar" | "tar.gz"
        stream: AsyncIterator[bytes]

DEFAULT_BASE_URL = "http://eaip-2.bocomm.com"
DEFAULT_DOWNLOAD_PATH = "/EAGP.EAGP-AGENT.V-1.0/bocomExportSkill.upload"
DEFAULT_LIST_PATH = "/EAGP.EAGP-AGENT.V-1.0/bocomListSkill.do"
DEFAULT_CONTENT_PATH = "/EAGP.EAGP-AGENT.V-1.0/bocomParseSkillPackageContent.do"

# 默认流式分块大小（64 KiB），与 ClawSkillHub 的 DEFAULT_CHUNK_SIZE 一致。
DEFAULT_CHUNK_SIZE = 64 * 1024

# Content-Type -> SkillArchive.format
_FORMAT_BY_CONTENT_TYPE = {
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/x-tar": "tar",
    "application/gzip": "tar.gz",
    "application/x-gzip": "tar.gz",
    "application/tar+gzip": "tar.gz",
}

# Content-Disposition 文件名后缀 -> SkillArchive.format
_EXT_FORMAT = re.compile(
    r"\.(?P<ext>zip|tar|tar\.gz|tgz)(?![\w.])",
    re.IGNORECASE,
)


def _format_from_filename(filename: str) -> str | None:
    """Map an archive filename to its :class:`SkillArchive` format."""
    match = _EXT_FORMAT.search(filename)
    if match is None:
        return None
    ext = match.group("ext").lower()
    if ext == "tgz":
        return "tar.gz"
    return ext


class JiaoXinSkillHub(HubBase):
    """A skill hub backed by the JiaoXin ``bocomExportSkill`` endpoint.

    .. code-block:: python

        hub = JiaoXinSkillHub(token="...")
        archive = await hub.download("OCR识别")
        async for chunk in archive.stream:
            ...  # 或直接 workspace.add_skill_archive(...) 落地工作区
    """

    def __init__(
        self,
        hub_id: str = "jiaoxin",
        display_name: str = "交心技能市场",
        description: str = "行内交心技能市场技能导出服务。",
        icon_url: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        download_path: str = DEFAULT_DOWNLOAD_PATH,
        list_path: str = DEFAULT_LIST_PATH,
        content_path: str = DEFAULT_CONTENT_PATH,
        token: str | None = None,
        timeout: float = 30.0,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        """Initialize the JiaoXin skill provider.

        Args:
            hub_id (`str`, defaults to `"jiaoxin"`):
                The stable identifier addressing this hub in the API
                routes.
            display_name (`str`, defaults to `"交心技能市场"`):
                The user-facing hub name.
            description (`str`, optional):
                The user-facing hub description.
            icon_url (`str | None`, optional):
                An image identifying the hub in the picker.
            base_url (`str`, defaults to `"http://eaip-2.bocomm.com"`):
                The base URL of the EAGP gateway.
            download_path (`str`, defaults to
                `"/EAGP.EAGP-AGENT.V-1.0/bocomExportSkill.upload"`):
                The export endpoint path under ``base_url``.
            list_path (`str`, defaults to
                `"/EAGP.EAGP-AGENT.V-1.0/bocomListSkill.do"`):
                The skill listing endpoint path under ``base_url``.
            token (`str | None`, optional):
                The ``guwp-token`` header value. Should come from
                configuration / environment, never hardcoded.
            timeout (`float`, defaults to `30.0`):
                The per-request timeout in seconds.
            chunk_size (`int`, defaults to `65536`):
                The number of bytes to yield per chunk.
        """
        super().__init__(hub_id, display_name, description, icon_url)
        self.base_url = base_url.rstrip("/")
        self.download_path = download_path
        self.list_path = list_path
        self.content_path = content_path
        self.token = token
        self.timeout = timeout
        self.chunk_size = chunk_size
        self._client: Any = None

    async def __aenter__(self) -> "JiaoXinSkillHub":
        """Open the HTTP client shared by every request.

        Returns:
            `JiaoXinSkillHub`:
                ``self``.
        """
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        """Close the shared HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> Any:
        """Return the shared client, opening one if never entered.

        Returns:
            `httpx.AsyncClient`:
                The client to issue requests with.
        """
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        """Build the request headers, including auth when available.

        Returns:
            `dict[str, str]`:
                The HTTP headers to attach to a request.
        """
        headers = {"Accept": "application/json;charset=utf-8"}
        if self.token:
            headers["guwp-token"] = self.token
        return headers

    @staticmethod
    def _build_req_message(
        name: str,
        namespace_slug: str,
        channel: str,
        version: str | None = None,
    ) -> dict[str, str]:
        """Build the form body the JiaoXin endpoint expects.

        Args:
            name (`str`):
                The skill display name, e.g. ``"OCR识别"``.
            namespace_slug (`str`):
                The skill namespace, e.g. ``"Global"``.
            channel (`str`):
                The channel code, e.g. ``"aioffice"``.
            version (`str | None`, optional):
                A specific skill version. ``None`` lets the upstream
                serve its default (latest).

        Returns:
            `dict[str, str]`:
                The multipart form fields — a single ``REQ_MESSAGE``
                carrying the JSON envelope.
        """
        param = {
            "name": name,
            "namespaceSlug": namespace_slug,
            "channel": channel,
        }
        if version is not None:
            param["version"] = version
        req_message = {
            "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
            "REQ_BODY": {"param": param},
        }
        return {"REQ_MESSAGE": json.dumps(req_message, ensure_ascii=False)}

    @staticmethod
    def _detect_format(headers: Any) -> str:
        """Infer the archive format from the response headers.

        ``Content-Type`` wins, then the ``Content-Disposition``
        filename extension, then the default ``zip``.

        Args:
            headers (`httpx.Headers`):
                The response headers.

        Returns:
            `str`:
                One of ``"zip"`` / ``"tar"`` / ``"tar.gz"``.
        """
        content_type = (headers.get("Content-Type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        if content_type in _FORMAT_BY_CONTENT_TYPE:
            return _FORMAT_BY_CONTENT_TYPE[content_type]

        disposition = headers.get("Content-Disposition") or ""
        filename_match = re.search(r'filename\*?=(?:"([^"]+)"|([^;]+))', disposition)
        if filename_match:
            filename = (filename_match.group(1) or filename_match.group(2)).strip()
            fmt = _format_from_filename(filename)
            if fmt is not None:
                return fmt

        return "zip"

    @staticmethod
    async def _drain(
        stack: AsyncExitStack,
        response: Any,
        chunk_size: int,
    ) -> AsyncIterator[bytes]:
        """Yield the response body, closing the response after.

        Args:
            stack (`AsyncExitStack`):
                Holds the open response, closed when the body is done.
            response (`httpx.Response`):
                The open response whose body is drained.
            chunk_size (`int`):
                The number of bytes to yield per chunk.

        Yields:
            `bytes`:
                The next chunk of the archive.
        """
        try:
            async for chunk in response.aiter_bytes(chunk_size):
                yield chunk
        finally:
            await stack.aclose()

    async def download(
        self,
        name: str,
        namespace_slug: str = "Global",
        channel: str = "aioffice",
        version: str | None = None,
    ) -> SkillArchive:
        """Stream a skill archive from the JiaoXin export endpoint.

        The response headers are awaited here — so an upstream failure
        raises before the caller commits to an install — while the body
        stays lazy, letting the archive be piped into a workspace
        without ever being held whole.

        Args:
            name (`str`):
                The skill display name, e.g. ``"OCR识别"``.
            namespace_slug (`str`, defaults to `"Global"`):
                The skill namespace.
            channel (`str`, defaults to `"aioffice"`):
                The channel code.
            version (`str | None`, optional):
                A specific skill version. ``None`` lets the upstream
                serve its default (latest).

        Returns:
            `SkillArchive`:
                The archive format and its byte stream.

        Raises:
            `HubError`:
                When the endpoint returns a non-success status code, or
                a JSON body (treated as an error envelope) instead of an
                archive.
        """
        url = f"{self.base_url}{self.download_path}"
        client = self._http()
        # Only the response is owned here — the client outlives every
        # download, so closing it would break the next one.
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(
                client.stream(
                    "POST",
                    url,
                    data=self._build_req_message(
                        name, namespace_slug, channel, version,
                    ),
                    headers=self._headers(),
                ),
            )
            if response.status_code >= 400:
                body = await response.aread()
                raise HubError(
                    self.hub_id,
                    response.status_code,
                    body.decode("utf-8", errors="replace"),
                )

            content_type = (response.headers.get("Content-Type") or "").lower()
            if "json" in content_type:
                # 2xx + JSON：按错误信封处理（本端点成功应答应为二进制
                # 压缩包；若联调发现 JSON 包裹成功载荷，在此补充解包）。
                body = await response.aread()
                raise HubError(
                    self.hub_id,
                    response.status_code,
                    body.decode("utf-8", errors="replace"),
                )

            return SkillArchive(
                self._detect_format(response.headers),
                self._drain(stack, response, self.chunk_size),
            )
        except BaseException:
            await stack.aclose()
            raise

    async def list_skills(
        self,
        *,
        keyword: str | None = None,
        name: str | None = None,
        displayName: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        labelSlugs: str | None = None,
        channel: str | None = None,
        sort: str | None = None,
        myOnly: bool | None = None,
        favoritesOnly: bool | None = None,
        installedOnly: bool | None = None,
        developer: str | None = None,
        loginName: str | None = None,
        source: str | None = None,
        publishedOnly: bool | None = None,
        page: int = 1,
        size: int = 20,
        startDate: str | None = None,
        endDate: str | None = None,
        updateStartDate: str | None = None,
        updateEndDate: str | None = None,
        arena: str | None = None,
        deptId: str | None = None,
        orders: list[dict[str, Any]] | None = None,
    ) -> dict:
        """分页查询交心技能市场的技能。

        调用上游 ``bocomListSkill.do``，返回上游**完整响应信封**原样
        字典（``RSP_BODY`` + ``RSP_HEAD``），不做成功/失败判定与字段
        映射——业务失败（``TRAN_SUCCESS=0``）也按原文返回，由调用方
        自行判读；上游新增字段自动透出。

        查询参数与上游 ``param`` 字段一一对应（名称完全一致），全部
        可选，仅非 None 项参与组装。

        Args:
            keyword (`str | None`, optional):
                全局关键字模糊搜索（匹配 name/displayName/description/
                displayDescription）。
            name (`str | None`, optional):
                技能名称精确匹配。
            displayName (`str | None`, optional):
                展示名称。
            status (`str | None`, optional):
                技能状态：DRAFT / PENDING_REVIEW / PUBLISHED /
                REJECTED / DELISTED。
            namespace (`str | None`, optional):
                命名空间 slug 精确匹配（如 ``"global"``）。
            labelSlugs (`str | None`, optional):
                标签 slug，逗号分隔，OR 语义。
            channel (`str | None`, optional):
                渠道 slug，逗号分隔，OR 语义。
            sort (`str | None`, optional):
                排序方式：``recommended`` / ``downloads`` / ``latest``。
            myOnly (`bool | None`, optional):
                是否仅查当前用户的技能。
            favoritesOnly (`bool | None`, optional):
                是否仅查当前用户收藏的技能。
            installedOnly (`bool | None`, optional):
                是否仅查已安装的技能。
            developer (`str | None`, optional):
                发布人用户ID。
            loginName (`str | None`, optional):
                发布人登录名。
            source (`str | None`, optional):
                来源筛选，逗号分隔，OR 语义。
            publishedOnly (`bool | None`, optional):
                是否仅查已发布且未下架的技能。
            page (`int`, defaults to `1`):
                页码。
            size (`int`, defaults to `20`):
                每页条数（不超过 100）。
            startDate (`str | None`, optional):
                创建时间范围-起始（如 ``"2025-01-01"``）。
            endDate (`str | None`, optional):
                创建时间范围-结束。
            updateStartDate (`str | None`, optional):
                更新时间范围-起始。
            updateEndDate (`str | None`, optional):
                更新时间范围-结束。
            arena (`str | None`, optional):
                运营场次过滤：OFFICIAL / TEST。
            deptId (`str | None`, optional):
                部门ID，用于 DEPT 类型可见性 SQL 过滤。
            orders (`list | None`, optional):
                排序条件，column 支持 createdAt/updatedAt/displayName 等。

        Returns:
            `dict`:
                上游完整响应信封原样内容（``RSP_BODY`` + ``RSP_HEAD``）。

        Raises:
            `HubError`:
                上游 HTTP 失败（状态码与响应体随异常透出），或上游
                返回非 JSON body。
        """
        # 参数名与上游 param 一致，仅过滤 None 项
        param: dict[str, Any] = {"page": page, "size": size}
        for key, value in (
            ("keyword", keyword),
            ("name", name),
            ("displayName", displayName),
            ("status", status),
            ("namespace", namespace),
            ("labelSlugs", labelSlugs),
            ("channel", channel),
            ("sort", sort),
            ("myOnly", myOnly),
            ("favoritesOnly", favoritesOnly),
            ("installedOnly", installedOnly),
            ("developer", developer),
            ("loginName", loginName),
            ("source", source),
            ("publishedOnly", publishedOnly),
            ("startDate", startDate),
            ("endDate", endDate),
            ("updateStartDate", updateStartDate),
            ("updateEndDate", updateEndDate),
            ("arena", arena),
            ("deptId", deptId),
            ("orders", orders),
        ):
            if value is not None:
                param[key] = value

        req_message = {
            "REQ_HEAD": {"TRAN_PROCESS": "bocomListSkill", "TRAN_ID": ""},
            "REQ_BODY": {"param": param},
        }
        url = f"{self.base_url}{self.list_path}"
        response = await self._http().post(
            url,
            data={"REQ_MESSAGE": json.dumps(req_message, ensure_ascii=False)},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise HubError(
                self.hub_id,
                response.status_code,
                response.text,
            )

        # 原样返回上游完整信封（RSP_BODY + RSP_HEAD），不做成功/失败
        # 判定与包装——业务失败（TRAN_SUCCESS=0）也由调用方按原文处理。
        try:
            return response.json()
        except ValueError as e:
            raise HubError(
                self.hub_id,
                response.status_code,
                f"upstream returned non-JSON body: {response.text}",
            ) from e

    async def get_skill_content(self, name: str) -> dict:
        """查询技能的 ``SKILL.md`` 内容。

        调用上游 ``bocomParseSkillPackageContent.do``（**REQ_MESSAGE
        表单形态**，与 list/download 一致），返回上游**完整响应信封**
        原样字典——``RSP_BODY.result`` 即 SKILL.md 文本；不做成功/失败
        判定与包装，业务失败（``TRAN_SUCCESS=0``）也按原文返回。

        Args:
            name (`str`):
                技能名（上游 ``skillName``），如 ``"mail-risk-scan"``。

        Returns:
            `dict`:
                上游完整响应信封原样内容。

        Raises:
            `HubError`:
                上游 HTTP 失败（状态码与响应体随异常透出），或上游
                返回非 JSON body。
        """
        req_message = {
            "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
            "REQ_BODY": {"param": {"skillName": name}},
        }
        url = f"{self.base_url}{self.content_path}"
        response = await self._http().post(
            url,
            data={"REQ_MESSAGE": json.dumps(req_message, ensure_ascii=False)},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise HubError(
                self.hub_id,
                response.status_code,
                response.text,
            )
        try:
            return response.json()
        except ValueError as e:
            raise HubError(
                self.hub_id,
                response.status_code,
                f"upstream returned non-JSON body: {response.text}",
            ) from e
