# -*- coding: utf-8 -*-
"""ELLM 凭证的配置查询与部分更新。

- ``GET  /model/credential?credential_id=...`` —— 按凭证 id 返回其实际
  存储配置（不含模型候选：凭证不绑定模型，候选模型由官方
  ``GET /model/?provider=...`` 提供）；
- ``PATCH /model/credential/{id}`` —— 只覆盖前端传入字段、其余保持原值
  的合并式更新（区别于官方 ``PATCH /credential/{id}`` 的整体替换）。
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


class CredentialConfigResponse(BaseModel):
    """按凭证 id 查询到的凭证实际配置。"""

    credential_id: str = Field(description="凭证 id。")
    data: dict[str, Any] = Field(
        description=(
            "该凭证实际存储的配置字段（type/name/base_url/scene_code/"
            "api_key_url/apikey_expires_at/...）。不含模型候选。"
        ),
    )


@credential_model_router.get(
    "/credential",
    response_model=CredentialConfigResponse,
    summary="Get a credential's effective configuration",
    description=(
        "Resolve the credential by id (ownership/sharing check) and "
        "return its stored configuration fields. Model candidates are "
        "served by the official ``GET /model/?provider=...`` — the "
        "credential itself does not bind a model."
    ),
)
async def get_credential_config(
    credential_id: str = Query(
        ...,
        description="The credential whose configuration to return.",
    ),
    user_id: str = Depends(get_current_user_id),
    access: ResourceAccessService = Depends(get_resource_access_service),
) -> CredentialConfigResponse:
    """返回凭证的实际配置。

    ``resolve_credential`` 校验归属/共享（不可见 → 404）；随后反序列化
    原始 payload 并返回其全部存储字段（与 ``PATCH`` 响应同构）。
    """
    record = await access.resolve_credential(user_id, credential_id)

    credential = CredentialFactory.from_dict(record.data)
    return CredentialConfigResponse(
        credential_id=record.data.get("id") or credential_id,
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


class ELLMCredentialPatchResponse(BaseModel):
    """部分更新后的凭证视图。"""

    credential_id: str = Field(description="凭证 id。")
    data: dict[str, Any] = Field(description="更新后的完整 payload data。")


@credential_model_router.patch(
    "/credential/{credential_id}",
    response_model=ELLMCredentialPatchResponse,
    summary="Partially update an ELLM credential",
    description=(
        "Merge only the fields sent in the request body into the stored "
        "credential payload — unpassed fields keep their current values "
        "(unlike the official ``PATCH /credential/{id}`` which replaces "
        "the whole payload). The merged result is re-validated as an "
        "``ELLMCredential`` (required fields must be present)."
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
    - 合并后整体重新校验（必填字段齐全等），非法 → 422；
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

    # 合并后整体校验（必填字段齐全等），非法 → 422。
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
