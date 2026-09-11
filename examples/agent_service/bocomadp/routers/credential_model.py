# -*- coding: utf-8 -*-
"""按 (X-User-ID, credential_id) 查询凭证 payload；并提供 ELLM 凭证部分更新。

- ``GET    /model/credential?credential_id=<id>``   查凭证 payload（**严格归属**）
- ``PATCH  /model/credential/{credential_id}``      部分更新（仅 ELLM）

用法::

    GET /model/credential?credential_id=<id>      # 身份取请求头 X-User-ID

归属口径（两接口不同，均为刻意设计）：

- ``GET``：**只认调用者自己的凭证**（``storage.get_credential``）——该接口
  会返回含明文 ``api_key`` 的完整 payload，因此不能走带"全局兜底"补丁的
  ``resolve_credential``；
- ``PATCH``：沿用 ``resolve_credential``（own / 共享可见即可更新），保持
  与原有行为一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from agentscope.app._service import ResourceAccessService
from agentscope.app.deps import (
    get_current_user_id,
    get_resource_access_service,
    get_storage,
)
from agentscope.app.storage import StorageBase
from agentscope.credential import CredentialFactory

credential_model_router = APIRouter(prefix="/model", tags=["credential-model"])


class CredentialPayloadResponse(BaseModel):
    """凭证 payload 查询响应。"""

    credential_id: str = Field(description="凭证 id。")
    data: dict[str, Any] = Field(description="凭证的完整 payload data。")


@credential_model_router.get(
    "/credential",
    response_model=CredentialPayloadResponse,
    summary="Get a credential payload by id",
    description=(
        "Look up the credential by ``(X-User-ID, credential_id)`` and "
        "return its full payload ``data``. Strictly scoped to the "
        "caller's own credentials — not found (or not owned) → 404."
    ),
)
async def get_credential_payload(
    credential_id: str = Query(
        ...,
        description="The credential to inspect.",
    ),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> CredentialPayloadResponse:
    """按 (X-User-ID, credential_id) 查询凭证 payload。

    **严格按调用者归属定位**（``storage.get_credential(user_id, id)``）：
    别的用户的凭证、以及"被共享给自己"的凭证都返回 404。

    这里刻意**不使用** ``ResourceAccessService.resolve_credential``：本
    仓库的 ``bocomadp/open_agent_access.py`` 给该方法打了"全局兜底"补丁
    （own / 共享均 miss 时按 id 直接全局查库，供开放交互模式下跨用户使用
    凭证），若走它则任意用户可用任意凭证 id 读到明文 payload。

    返回值 ``data`` 为完整 payload（含 ``api_key`` 明文），仅供内部排查/
    管理使用，注意不要在前端或日志中裸奔。
    """
    record = await storage.get_credential(user_id, credential_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Credential {credential_id!r} not found for "
                f"user {user_id!r}."
            ),
        )
    credential = CredentialFactory.from_dict(record.data)
    return CredentialPayloadResponse(
        credential_id=credential.id,
        data=_dump_credential_data(credential),
    )


class ELLMCredentialPatch(BaseModel):
    """部分更新 ELLM 凭证的请求体——只包含要修改的字段。"""

    data: dict[str, Any] = Field(
        description=(
            "要更新的字段子集（如 {\"api_key\": \"sk-new\"}）；未传的字段 "
            "保持原值不变。"
        ),
    )


class ELLMCredentialPatchResponse(CredentialPayloadResponse):
    """部分更新后的凭证视图（与查询响应同构）。"""


@credential_model_router.patch(
    "/credential/{credential_id}",
    response_model=ELLMCredentialPatchResponse,
    summary="Partially update an ELLM credential",
    description=(
        "Merge only the fields sent in the request body into the stored "
        "credential payload — unpassed fields keep their current values "
        "(unlike the official ``PATCH /credential/{id}`` which replaces "
        "the whole payload). The merged result is re-validated as an "
        "``ELLMCredential`` (e.g. ``model`` must stay in candidates)."
    ),
)
async def patch_ellm_credential(
    credential_id: str,
    body: ELLMCredentialPatch,
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
    storage: StorageBase = Depends(get_storage),
) -> ELLMCredentialPatchResponse:
    """部分修改 ELLM 凭证：只覆盖前端传入的字段，其余保持原值。

    - ``resolve_credential`` 校验归属/共享，不可见 → 404；
    - 非 ``bocom_ellm_credential`` 类型 → 400；
    - 合并后整体重新校验（``model`` 必须仍在候选等），非法 → 422；
    - ``id``/``type`` 永远保持原值，不可被覆盖。
    """
    record = await access.resolve_credential(user_id, credential_id)

    existing = dict(record.data or {})
    if existing.get("type") != "bocom_ellm_credential":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Credential {credential_id!r} is type "
                f"{existing.get('type')!r}, not 'bocom_ellm_credential'."
            ),
        )

    # 只覆盖前端传入的字段；id/type 强制保持原值。
    merged = {**existing, **body.data}
    merged["id"] = existing.get("id") or credential_id
    merged["type"] = "bocom_ellm_credential"

    # 合并后整体校验（model 候选、必填字段等），非法 → 422。
    credential = CredentialFactory.from_dict(merged)
    credential.id = existing.get("id") or credential_id

    new_id = await storage.upsert_credential(user_id, credential)
    return ELLMCredentialPatchResponse(
        credential_id=new_id,
        data=_dump_credential_data(credential),
    )


def _dump_credential_data(credential: Any) -> dict[str, Any]:
    """序列化凭证为 payload data（SecretStr 解明文）。"""
    from agentscope.app.storage._utils import _dump_with_secrets

    return _dump_with_secrets(credential)
