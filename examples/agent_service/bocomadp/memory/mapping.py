# -*- coding: utf-8 -*-
"""表记录 → SDK 请求对象映射：注册 / 落库 / 检索请求的唯一构造处。

调用方（platform 门面调用点 / extractor / middleware / routers）统一经本模块
构造 SDK 请求对象，字段映射只有一处。

- ``last_msg_id`` 由调用方传入（提取器侧 = 本批切片末条被发送的
  assistant 消息 id），本层不做默认，防止落库幂等锚点漂移；该值作为
  平台请求的 ``run_id`` 传入。
- ``infer`` 为 agent 级配置直传——Record 侧存 "0"/"1" 字符串、SDK 形参
  为 int，本层做 int() 适配（不写死默认值）。
- ``memory_prompt`` 空串 → None（吃平台默认）。
"""
from __future__ import annotations

from euvd_memory_pysdk.models import (
    Message,
    RegisterAgentRequest,
    SaveMemoriesStandardRequest,
    SearchMemoryRequest,
)

from .storage._models import MemoryAgentConfigRecord


def build_register_request(
    record: MemoryAgentConfigRecord,
    agent_name: str,
) -> RegisterAgentRequest:
    """构造注册请求：可选字段空值透传 None（平台默认）。"""
    return RegisterAgentRequest(
        agent_id=record.agent_id,
        agent_name=agent_name,
        agent_plat=record.agent_plat,
        memory_prompt=(record.memory_prompt or None),
        message_type=record.message_type,
    )


def build_save_request(
    cfg: MemoryAgentConfigRecord,
    user_id: str,
    messages: list[tuple[str, str]],
    last_msg_id: str,
) -> SaveMemoriesStandardRequest:
    """构造落库请求：完整轮次消息切片 + 落库幂等锚点。

    ``last_msg_id``：本批**切片后**最后一条被发送的 assistant 消息 id
    （不是会话最新一条——尾部悬空消息已被切片截掉），随 ``Msg.id``
    落库持久，同批重发值不变。该值作为平台请求的 ``run_id`` 传入：
    平台侧语义是「一次落库运行的标识」（分组 / 去重 / 按运行检索），
    与框架 ``agentscope.app`` 事件流的同名 ``run_id`` 无关。
    """
    return SaveMemoriesStandardRequest(
        messages=[Message(role=role, content=content) for role, content in messages],
        user_code=user_id,
        agent_id=cfg.agent_id,
        agent_plat=cfg.agent_plat,
        run_id=last_msg_id,  # 平台字段名，唯一映射点
        infer=int(cfg.infer),
    )


def build_search_request(
    cfg: MemoryAgentConfigRecord,
    user_id: str,
    keyword: str,
) -> SearchMemoryRequest:
    """构造检索请求（top_k 用 agent 级配置，调用方不另行覆盖）。"""
    return SearchMemoryRequest(
        user_code=user_id,
        agent_id=cfg.agent_id,
        agent_plat=cfg.agent_plat,
        keyword=keyword,
        top_k=cfg.top_k,
    )
