# -*- coding: utf-8 -*-
"""开放智能体交互：任意用户可与任意智能体对话（列表接口保持 owner 隔离）。

按「框架源码不动、企业逻辑进 bocomadp」约定实现（与 ``team_toolkit`` /
``agent_list_sort`` 同一模式）：

1. :func:`get_agent_global`：跨 owner 按 id 查智能体（仅 SQL 主存储支持），
   team worker（``source=="team"``）不参与全局兜底，保持仅 owner 可见。
2. :func:`patch_open_agent_access`：包装
   :meth:`agentscope.app._service._access.ResourceAccessService.resolve_agent`，
   owner / 共享引用均 miss 后回退全局查询——放开 ``_run_impl``、
   创建会话（``POST /sessions``）、会话列表等对话链路的 agent 归属校验。
3. :func:`patch_open_session_credentials`：放开原生创建/更新会话接口
   （``POST /sessions`` / ``PUT /sessions/{id}``）的凭证归属校验——
   ``chat_model_config`` 等引用的 credential 不再要求对调用者可见。
4. :func:`patch_open_runtime_credentials`：放开运行时凭证解析
   （chat / embedding / TTS 模型构建时经
   :meth:`ResourceAccessService.resolve_credential`）的归属校验——
   own / 共享引用均 miss 后回退全局查询，密钥可跨用户使用。

列表接口（``list_resource`` / ``list_agents``）不受影响：它们不走
``resolve_agent``，仍只返回调用者自己的智能体；凭证列表接口同样
不受影响（走 ``get_resource``，共享视图仍掩码）。

启动时调用一次（幂等，重复调用无害）::

    from bocomadp.open_agent_access import (
        patch_open_agent_access,
        patch_open_session_credentials,
        patch_open_runtime_credentials,
    )
    patch_open_agent_access()
    patch_open_session_credentials()
    patch_open_runtime_credentials()
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger("bocomadp.open_agent_access")


async def get_agent_global(storage: Any, agent_id: str) -> Any | None:
    """跨 owner 按 id 查智能体；非 SQL 主存储时返回 ``None``。

    依赖框架 SQL 存储的内部模型与转换函数（同一仓库、框架源码不改，
    仅引用）：``AgentRow`` 主键为全局唯一 id，``storage._session()``
    返回新 AsyncSession。Redis 等按 user 分片的主存储无跨 owner
    索引，返回 ``None``（兜底静默失效，由调用方回退 404）。
    """
    try:
        from agentscope.app.storage import AgentRecord
        from agentscope.app.storage._sql._mappers import _to_record
        from agentscope.app.storage._sql._tables import AgentRow
    except ImportError:
        return None

    session_factory = getattr(storage, "_session", None)
    if session_factory is None:
        logger.warning(
            "open_agent_access: storage %r has no SQL session factory; "
            "cross-owner agent lookup disabled",
            type(storage).__name__,
        )
        return None
    try:
        async with session_factory() as sess:
            row = await sess.get(AgentRow, agent_id)
    except Exception:  # noqa: BLE001 —— 兜底查询，失败不阻断对话
        logger.exception("open_agent_access: global agent lookup failed")
        return None
    if row is None:
        return None
    return _to_record(row, AgentRecord)


def patch_open_agent_access() -> None:
    """包装 ``ResourceAccessService.resolve_agent``（幂等）。

    owner / 共享引用均 miss 后回退全局查询：任意用户可对话任意智能体
    （除 team worker）。创建会话（``POST /sessions``）等同走
    ``resolve_agent`` 的链路一并放开；列表接口不受影响。
    """
    from agentscope.app._service._access import ResourceAccessService

    if getattr(
        ResourceAccessService.resolve_agent,
        "_open_agent_patched",
        False,
    ):
        return

    original = ResourceAccessService.resolve_agent

    async def resolve_agent_open(
        self: Any,
        viewer_id: str,
        agent_id: str,
    ) -> Any:
        try:
            return await original(self, viewer_id, agent_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        storage = getattr(self, "_storage", None)
        if storage is None:
            raise
        record = await get_agent_global(storage, agent_id)
        if record is not None and record.source != "team":
            return record
        raise

    resolve_agent_open._open_agent_patched = True  # type: ignore[attr-defined]
    ResourceAccessService.resolve_agent = resolve_agent_open  # type: ignore[assignment]
    logger.info(
        "patched %s.resolve_agent with open-agent fallback",
        ResourceAccessService.__name__,
    )


async def get_credential_global(storage: Any, credential_id: str) -> Any | None:
    """跨 owner 按 id 查凭证；非 SQL 主存储时返回 ``None``。

    与 :func:`get_agent_global` 同模式：``CredentialRow`` 主键为全局
    唯一 id，``storage._session()`` 返回新 AsyncSession；Redis 等按
    user 分片的主存储返回 ``None``（兜底静默失效）。返回原始
    ``CredentialRecord``（含明文 ``data``），仅供运行时模型构建使用。
    """
    try:
        from agentscope.app.storage import CredentialRecord
        from agentscope.app.storage._sql._mappers import _to_record
        from agentscope.app.storage._sql._tables import CredentialRow
    except ImportError:
        return None

    session_factory = getattr(storage, "_session", None)
    if session_factory is None:
        logger.warning(
            "open_agent_access: storage %r has no SQL session factory; "
            "cross-owner credential lookup disabled",
            type(storage).__name__,
        )
        return None
    try:
        async with session_factory() as sess:
            row = await sess.get(CredentialRow, credential_id)
    except Exception:  # noqa: BLE001 —— 兜底查询，失败不阻断对话
        logger.exception("open_agent_access: global credential lookup failed")
        return None
    if row is None:
        return None
    return _to_record(row, CredentialRecord)


def patch_open_runtime_credentials() -> None:
    """包装 ``ResourceAccessService.resolve_credential``（幂等）。

    own / 共享引用均 miss 后回退全局查询：任意用户运行时的模型构建
    （chat / embedding / TTS）可引用任意凭证，密钥跨用户使用。
    凭证列表接口不受影响（走 ``get_resource``，共享视图仍掩码）。
    """
    from agentscope.app._service._access import ResourceAccessService

    if getattr(
        ResourceAccessService.resolve_credential,
        "_open_credential_patched",
        False,
    ):
        return

    original = ResourceAccessService.resolve_credential

    async def resolve_credential_open(
        self: Any,
        viewer_id: str,
        credential_id: str,
    ) -> Any:
        try:
            return await original(self, viewer_id, credential_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        storage = getattr(self, "_storage", None)
        if storage is None:
            raise
        record = await get_credential_global(storage, credential_id)
        if record is not None:
            return record
        raise

    resolve_credential_open._open_credential_patched = True  # type: ignore[attr-defined]
    ResourceAccessService.resolve_credential = (  # type: ignore[assignment]
        resolve_credential_open
    )
    logger.info(
        "patched %s.resolve_credential with open-credential fallback",
        ResourceAccessService.__name__,
    )


def patch_open_session_credentials() -> None:
    """放开原生创建/更新会话接口的凭证归属校验（幂等）。

    原生 ``create_session`` / ``update_session`` 会校验
    ``chat_model_config`` 等引用的 credential 必须对调用者可见
    （``_ensure_credential_exists``，own or shared，否则 404）。开放
    交互模式下任意用户可与任意智能体建会话，会话配置里引用的凭证
    不要求归属可见——直接替换为 no-op。

    替换的是 ``_router/_session.py`` 的模块级私有函数：端点函数体内
    按模块全局名查找，替换模块属性即可生效，不改框架源码。
    """
    import agentscope.app._router._session as _session_mod

    if getattr(_session_mod, "_credential_check_open", False):
        return

    async def _noop_credential_check(
        access: Any,
        user_id: str,
        config: Any,
    ) -> None:
        del access, user_id, config  # no-op：开放交互模式不校验凭证归属

    _session_mod._ensure_credential_exists = _noop_credential_check
    _session_mod._credential_check_open = True
    logger.info(
        "patched %s._ensure_credential_exists with open no-op",
        _session_mod.__name__,
    )


__all__ = [
    "patch_open_agent_access",
    "patch_open_session_credentials",
    "patch_open_runtime_credentials",
    "get_agent_global",
    "get_credential_global",
]
