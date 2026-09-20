# -*- coding: utf-8 -*-
"""跨 owner 凭证全局查询（运行时兜底）。

按 id 跨 owner 查凭证记录（仅供运行时模型构建使用，返回含明文 ``data``
的 ``CredentialRecord``）；非 SQL 主存储（Redis 等按 user 分片的主存储
无跨 owner 索引）时返回 ``None``，由调用方静默回退。依赖框架 SQL 存储的
内部模型与转换函数（仅引用，不改框架源码）。
"""
from __future__ import annotations

import logging
from typing import Any

# SQL 存储内部模型/转换函数仅在安装 SQL 依赖时可用：缺失（如纯 Redis
# 主存储的 SDK 环境）时降级为 None，跨 owner 查询静默失效。
try:  # noqa: E402
    from agentscope.app.storage import CredentialRecord
    from agentscope.app.storage._sql._mappers import _to_record
    from agentscope.app.storage._sql._tables import CredentialRow
except ImportError:  # pragma: no cover — SQL 依赖未安装
    CredentialRecord = None
    _to_record = None
    CredentialRow = None

logger = logging.getLogger("providers._global_lookup")


async def get_credential_global(storage: Any, credential_id: str) -> Any | None:
    """跨 owner 按 id 查凭证；非 SQL 主存储时返回 ``None``。

    ``CredentialRow`` 主键为全局唯一 id，``storage._session()`` 返回新
    AsyncSession；Redis 等按 user 分片的主存储返回 ``None``（兜底静默
    失效）。返回原始 ``CredentialRecord``（含明文 ``data``），仅供运行时
    模型构建使用。
    """
    if CredentialRow is None or _to_record is None or CredentialRecord is None:
        return None

    session_factory = getattr(storage, "_session", None)
    if session_factory is None:
        logger.warning(
            "providers: storage %r has no SQL session factory; "
            "cross-owner credential lookup disabled",
            type(storage).__name__,
        )
        return None
    try:
        async with session_factory() as sess:
            row = await sess.get(CredentialRow, credential_id)
    except Exception:  # noqa: BLE001 —— 兜底查询，失败不阻断对话
        logger.exception("providers: global credential lookup failed")
        return None
    if row is None:
        return None
    return _to_record(row, CredentialRecord)

__all__ = ["get_credential_global"]
