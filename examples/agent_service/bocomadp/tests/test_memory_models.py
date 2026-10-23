# -*- coding: utf-8 -*-
"""数据模型测试：两个 Record + MemoryGlobalConfig（Task 3，§6/D4/D9/D10）。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path


import pytest
from pydantic import ValidationError

from bocomadp.memory.config import GLOBAL_CONFIG_KEY, MemoryGlobalConfig
from bocomadp.memory.storage._models import (
    HEARTBEAT_TZ,
    MemoryAgentConfigRecord,
    MemorySessionStateRecord,
)


class TestAgentConfigRecord:
    def test_defaults(self):
        r = MemoryAgentConfigRecord(agent_id="a1")
        assert r.agent_id == "a1"
        assert r.injection_enabled is False
        assert r.extraction_enabled is False
        assert r.switch_epoch == 0
        assert r.memory_type == "0"
        assert r.update_rounds == 10
        assert r.memory_prompt == ""
        assert r.top_k == 5
        assert r.agent_plat == 0
        assert r.message_type == 0
        assert r.infer == "1"          # D10：原 infer_flag，默认 "1"=提取
        assert r.caller == ""

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            MemoryAgentConfigRecord(agent_id="a1", unknown_field=1)


class TestSessionStateRecord:
    def test_record_id_concat(self):
        r = MemorySessionStateRecord(
            user_id="u1", agent_id="a1", session_id="s1",
            last_active_at=1.0, turns=2,
            cursor_created_at="2026-01-01", cursor_msg_id="m9",
        )
        assert r.record_id == "u1:a1:s1"
        assert r.turns == 2

    def test_last_active_dt_utc_display_only(self):
        """心跳展示属性：epoch 秒 → UTC（固定 +0），且不入 payload。"""
        r = MemorySessionStateRecord(
            user_id="u1", agent_id="a1", session_id="s1",
            last_active_at=0.0, turns=0,
        )
        # epoch 0 即 1970-01-01T00:00:00Z
        assert r.last_active_dt == datetime(
            1970, 1, 1, 0, 0, tzinfo=HEARTBEAT_TZ
        )
        assert r.last_active_dt.utcoffset() == timedelta(0)
        # 展示属性不污染存储契约（payload 仍只有 float）
        assert "last_active_dt" not in r.model_dump()
        assert r.model_dump()["last_active_at"] == 0.0

    def test_extra_forbid(self):
        with pytest.raises(ValidationError):
            MemorySessionStateRecord(user_id="u", agent_id="a", session_id="s", foo=1)

    def test_last_active_at_required(self):
        """心跳必填：payload 不含它，漏传即 fail-fast（防静默取 0 → 误判超龄）。"""
        with pytest.raises(ValidationError):
            MemorySessionStateRecord(
                user_id="u", agent_id="a", session_id="s"
            )

    def test_turns_required(self):
        """turns 必填：payload 不含它（列是真源），漏传即 fail-fast。

        若保留默认 0，"漏 merge"会静默通过 → 该会话永不满足
        ``turns >= 1`` → 提取再也不触发（最隐蔽的故障形态）。
        """
        with pytest.raises(ValidationError):
            MemorySessionStateRecord(
                user_id="u", agent_id="a", session_id="s", last_active_at=1.0
            )


class TestGlobalConfig:
    def test_defaults(self):
        c = MemoryGlobalConfig()
        assert c.idle_minutes == 60
        assert c.sweep_interval_seconds == 300
        assert c.max_tokens == 90000
        assert c.state_ttl_days == 7

    def test_state_ttl_secs(self):
        c = MemoryGlobalConfig(state_ttl_days=2)
        assert c.state_ttl_secs == 2 * 86400

    def test_global_config_key(self):
        assert GLOBAL_CONFIG_KEY == "memory"
