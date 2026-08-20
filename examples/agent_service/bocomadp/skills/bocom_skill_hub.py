# -*- coding: utf-8 -*-
"""Bocom skill hub 提供者。

对接 Bocom 技能服务（EAGP.EAGP-AGENT.V-1.0）的目录查询与下载接口，
通过 :class:`~agentscope.app.hub._skill._base.SkillHubBase` 暴露，
使 Web UI 与 workspace 流程将其视为普通 skill hub。

当前实现两个端点（其余暂不实现）：

    POST {base}/bocomListSkill.do          → 目录查询（REQ_MESSAGE）
    POST {base}/bocomExportSkill.upload    → 下载 zip（REQ_MESSAGE）

服务地址从环境变量 ``BOCOMADP_BOCOM_SKILLHUB_URL`` 读取（默认
``http://eaip-2.bocomm.com/EAGP.EAGP-AGENT.V-1.0``）；认证头
``guwp-token`` 由调用方逐请求传入。

接入方式（main.py）：::

    skill_hubs=[..., BocomSkillHub(hub_id="bocom")]

``hub_id`` 对应框架通用 hub 路由；或调整路由中的 hub key。
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

#: 默认流式块大小（64 KiB）。
DEFAULT_CHUNK_SIZE = 64 * 1024

#: 默认服务地址（未配置 ``BOCOMADP_BOCOM_SKILLHUB_URL`` 时使用）。
DEFAULT_BASE_URL = "http://eaip-2.bocomm.com/EAGP.EAGP-AGENT.V-1.0"


def _default_base_url() -> str:
    """从环境变量读取 Bocom skillhub 地址（兼容 ``.env``），带默认值。

    环境变量未设置或为空字符串时，回退到默认地址。
    """
    return os.environ.get("BOCOMADP_BOCOM_SKILLHUB_URL", "").strip() or DEFAULT_BASE_URL


class BocomSkillHub(SkillHubBase):
    """对接 Bocom 技能服务的 skill hub（查询 + 下载）。

    .. code-block:: python

        hub = BocomSkillHub()                 # base_url 取环境变量
        page = await hub.list_skills(user_id="zy", keyword="excel")
        archive = await hub.download(user_id="zy", name="data-tag")
    """

    def __init__(
        self,
        hub_id: str = "bocom",
        display_name: str = "Bocom SkillHub",
        description: str = "Bocom 技能目录",
        icon_url: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        api_token: str | None = None,
    ) -> None:
        """初始化 Bocom skillhub 提供者。

        Args:
            hub_id (`str`): 路由中寻址该 hub 的稳定 id。
            display_name (`str`): 用户可见名称。
            description (`str`): 用户可见描述。
            icon_url (`str | None`): hub 图标。
            base_url (`str | None`): 服务地址；``None`` 时取
                ``BOCOMADP_BOCOM_SKILLHUB_URL``（或默认值）。
            timeout (`float`): 单请求超时（秒）。
            api_token (`str | None`): 初始 ``guwp-token``，可后续通过
                :meth:`set_token` 更新。
        """
        super().__init__(hub_id, display_name, description, icon_url)
        self.base_url = (base_url or _default_base_url()).rstrip("/")
        self.timeout = timeout
        self._client: "httpx.AsyncClient | None" = None
        self._guwp_token = api_token or ""

    def set_token(self, token: str | None) -> None:
        """更新目录/下载请求使用的 ``guwp-token`` 请求头。

        可逐请求调用；未设置时请求不带 token 头。
        """
        self._guwp_token = token or ""

    # ── 生命周期 ────────────────────────────────────────────────

    async def __aenter__(self) -> "BocomSkillHub":
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

    def _headers(self) -> dict[str, str]:
        """构造请求头（含 Content-Type 与可选的 guwp-token）。"""
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "User-Agent": "PostmanRuntime-ApipostRuntime/1.1.0",
        }
        if self._guwp_token:
            headers["guwp-token"] = self._guwp_token
        return headers

    # ── SkillHubBase ─────────────────────────────────────────────

    async def list_skills(
        self,
        user_id: str,
        *,
        keyword: str = "",
        status: str = "PUBLISHED",
        namespace: str = "global",
        labelSlugs: str = "",
        page: int = 1,
        myOnly: bool = True,
        size: int = 10,
    ) -> SkillHubPage:
        """浏览目录（POST ``bocomListSkill.do``，body 为 ``REQ_MESSAGE``）。

        参数与上游 curl 请求完全一致：``keyword`` / ``status`` /
        ``namespace`` / ``labelSlugs`` / ``page`` / ``myOnly`` / ``size``。

        ``user_id`` 参数为框架层调用者标识。
        """
        import json

        param = {
            "keyword": keyword,
            "status": status,
            "namespace": namespace,
            "labelSlugs": labelSlugs,
            "page": page,
            "myOnly": myOnly,
            "size": size,
        }

        payload = {
            "REQ_HEAD": {
                "TRAN_PROCESS": "bocomListSkill",
                "TRAN_ID": "",
            },
            "REQ_BODY": {"param": param},
        }
        url = f"{self.base_url}/bocomListSkill.do"
        try:
            resp = await self._http().post(
                url,
                data={"REQ_MESSAGE": json.dumps(payload, ensure_ascii=False)},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            status_code = getattr(getattr(e, "response", None), "status_code", 0)
            raise HubError(self.hub_id, status_code, str(e)) from e

        items, total = self._extract_items(data)
        cards = [
            self._to_card(item)
            for item in items
            if isinstance(item, dict) and self._card_name(item)
        ]
        return SkillHubPage(
            cards=cards,
            next_cursor=None,
            total=total,
        )

    async def get_skill(self, user_id: str, card_id: str) -> SkillCard:
        """尚未实现。

        Bocom 服务当前只使用目录与下载端点。

        Raises:
            NotImplementedError: 当前恒抛。
        """
        raise NotImplementedError(
            "BocomSkillHub.get_skill is not implemented yet — the Bocom "
            "service exposes catalog and download endpoints only.",
        )

    async def download(
        self,
        user_id: str,
        *,
        name: str,
        namespaceSlug: str = "Global",
    ) -> SkillArchive:
        """导出技能归档（POST ``bocomExportSkill.upload``，body 为 ``REQ_MESSAGE``）。

        ``REQ_BODY.param`` 与上游 curl 请求对齐（``name`` / ``namespaceSlug``）；
        响应体保持惰性，归档可被直接管道送入 workspace 而无需整体驻留内存。
        """
        import json

        payload = {
            "REQ_HEAD": {"TRAN_PROCESS": "", "TRAN_ID": ""},
            "REQ_BODY": {
                "param": {
                    "name": name,
                    "namespaceSlug": namespaceSlug,
                },
            },
        }
        url = f"{self.base_url}/bocomExportSkill.upload"
        headers = dict(self._headers())
        headers["Accept"] = "application/json;charset=utf-8"
        headers["Accept-Encoding"] = "gzip, deflate"

        client = self._http()
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(
                client.stream(
                    "POST",
                    url,
                    data={"REQ_MESSAGE": json.dumps(payload, ensure_ascii=False)},
                    headers=headers,
                ),
            )
            if response.status_code == 404:
                raise KeyError(name)
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

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _extract_items(data: Any) -> tuple[list, int | None]:
        """从响应中宽容提取目录列表与总数。

        兼容 ``RSP_BODY.result.list``、``data.items``、``data.list`` 等
        常见结构；总数取 ``total`` / ``totalCount`` / ``count``（含
        ``pageCond`` 内）。
        """
        if isinstance(data, list):
            return data, None
        if not isinstance(data, dict):
            return [], None

        node = data
        # 逐层深入常见 wrapper（RSP_BODY → result 等多层）。
        for _ in range(5):
            for wrapper in ("RSP_BODY", "data", "result", "content", "body"):
                if isinstance(node.get(wrapper), (dict, list)):
                    node = node[wrapper]
                    break
            else:
                break

        if isinstance(node, list):
            return node, None
        if not isinstance(node, dict):
            return [], None

        for items_key in ("items", "list", "records", "rows", "content", "skills"):
            items = node.get(items_key)
            if isinstance(items, list):
                total = None
                # 总数可能在 node 直接或 pageCond 内。
                for total_key in ("total", "totalCount", "count"):
                    if isinstance(node.get(total_key), int):
                        total = node[total_key]
                        break
                if total is None:
                    page_cond = node.get("pageCond")
                    if isinstance(page_cond, dict):
                        for total_key in ("total", "totalCount", "count"):
                            if isinstance(page_cond.get(total_key), int):
                                total = page_cond[total_key]
                                break
                return items, total
        return [], None

    @staticmethod
    def _card_name(item: dict) -> str | None:
        """从一条目录记录提取 skill 名称（即下载 URL 的 data-tag）。"""
        for key in ("name", "skillName", "skill_name", "slug", "title"):
            value = item.get(key)
            if value:
                return str(value)
        return None

    def _to_card(self, item: dict) -> SkillCard:
        """由一条目录记录构造 :class:`SkillCard`。

        ``namespaceName`` 提取到 ``tags``，完整记录透传 ``metadata``。
        """
        name = self._card_name(item)
        card_id = item.get("id") or item.get("skillId") or name
        description = (
            item.get("summary")
            or item.get("description")
            or item.get("desc")
            or ""
        )
        tags: list[str] = []
        if item.get("namespaceName"):
            tags.append(str(item["namespaceName"]))
        return SkillCard(
            hub_id=self.hub_id,
            id=str(card_id),
            name=name or "",
            description=str(description),
            tags=tags,
            metadata={k: v for k, v in item.items()},
        )

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
