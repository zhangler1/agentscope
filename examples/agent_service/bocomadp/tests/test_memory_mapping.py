# -*- coding: utf-8 -*-
"""mapping 测试（Task 8，§4.2）：字段映射与构造策略断言。"""
import sys
from pathlib import Path


from bocomadp.memory.mapping import (
    build_register_request,
    build_save_request,
    build_search_request,
)
from bocomadp.memory.storage._models import MemoryAgentConfigRecord


def _record(**kw) -> MemoryAgentConfigRecord:
    return MemoryAgentConfigRecord(agent_id="a1", **kw)


class TestSaveRequest:
    def test_messages_and_run_id(self):
        rec = _record(infer="1", agent_plat=1)
        msgs = [("user", "你好"), ("assistant", "你好，我是甲")]
        req = build_save_request(rec, "u1", msgs, "msg-a9")
        # D3：run_id 直传调用方锚点（末条 assistant 消息 id）
        assert req.run_id == "msg-a9"
        # 完整轮次消息切片（非单条）
        assert [(m.role, m.content) for m in req.messages] == msgs
        # data_source / memory_type 不传 → 吃 SDK 默认 int 1
        assert req.data_source == 1
        assert req.memory_type == 1
        # infer 直传（str → int 适配，D10）
        assert req.infer == 1
        rec0 = _record(infer="0")
        req0 = build_save_request(rec0, "u1", [("user", "x")], "r0")
        assert req0.infer == 0
        # 身份与路由字段
        assert req.user_code == "u1"
        assert req.agent_id == "a1"
        assert req.agent_plat == 1

    def test_empty_messages_allowed(self):
        # 空切片构造不抛（由提取器保证非空，本层不做防御）
        req = build_save_request(_record(), "u1", [], "r1")
        assert req.messages == []


class TestSearchRequest:
    def test_fields(self):
        rec = _record(agent_plat=1, top_k=5)
        req = build_search_request(rec, "u1", "查找")
        assert req.keyword == "查找"
        assert req.user_code == "u1"
        assert req.agent_id == "a1"
        assert req.agent_plat == 1
        assert req.top_k == 5  # agent 级配置直传


class TestRegisterRequest:
    def test_fields_and_none_passthrough(self):
        rec = _record(agent_plat=1, memory_prompt="", message_type=1)
        req = build_register_request(rec, "智能体甲")
        assert req.agent_id == "a1"
        assert req.agent_name == "智能体甲"
        assert req.agent_plat == 1
        assert req.memory_prompt is None      # 空串 → None（平台默认）
        assert req.message_type == 1

    def test_prompt_passthrough(self):
        rec = _record(
            agent_plat=1,
            memory_prompt="你是记忆管家",
            message_type=1,
        )
        req = build_register_request(rec, "甲")
        assert req.memory_prompt == "你是记忆管家"
