# -*- coding: utf-8 -*-
"""``/sessions/limit`` 会话名改写逻辑的单元测试。

只测纯函数（``_derive_session_title`` / ``_extract_message_text`` /
默认时间名正则），不起 DB —— 路由层的行为由真机验证兜底。

跑法（仓库根目录）::

    python -m pytest examples/agent_service/bocomadp/tests/test_session_title.py -v
"""

import json

from bocomadp.routers.session_usage import (
    _DEFAULT_NAME_RE,
    _derive_session_title,
    _extract_message_text,
)


def _user_msg(content, role="user"):
    return {"role": role, "content": content}


class TestDefaultNameRegex:
    """默认时间名识别：只有框架缺省形态才允许改写。"""

    def test_matches_framework_default(self):
        assert _DEFAULT_NAME_RE.match("2026-09-14 15:13:26")

    def test_rejects_custom_names(self):
        assert not _DEFAULT_NAME_RE.match("你好")
        assert not _DEFAULT_NAME_RE.match("")
        assert not _DEFAULT_NAME_RE.match("2026-09-14")  # 无时分秒
        assert not _DEFAULT_NAME_RE.match("hi 2026-09-14 15:13:26")


class TestExtractMessageText:
    """Msg.content 两种形态的文本提取。"""

    def test_str_content(self):
        assert _extract_message_text("你好") == "你好"

    def test_list_content_text_blocks(self):
        content = [
            {"type": "text", "text": "帮"},
            {"type": "text", "text": "我查"},
            {"type": "image", "url": "x.png"},  # 非文本块跳过
        ]
        assert _extract_message_text(content) == "帮\n我查"

    def test_unsupported_shape(self):
        assert _extract_message_text(None) == ""
        assert _extract_message_text(123) == ""


class TestDeriveSessionTitle:
    """首条用户输入 → 会话名。"""

    def test_first_user_message_wins(self):
        payloads = [
            _user_msg("你好"),
            _user_msg("第二条不该被选中"),
        ]
        assert _derive_session_title(payloads) == "你好"

    def test_skips_non_user_messages(self):
        payloads = [
            {"role": "assistant", "content": "我是助手"},
            _user_msg("你好"),
        ]
        assert _derive_session_title(payloads) == "你好"

    def test_collapses_whitespace(self):
        assert _derive_session_title([_user_msg("第一行\n  第二行\ttab")]) == (
            "第一行 第二行 tab"
        )

    def test_truncates_long_input(self):
        title = _derive_session_title([_user_msg("长" * 50)])
        assert len(title) == 30 + 1  # 30 字符 + "…"
        assert title.endswith("…")

    def test_empty_inputs(self):
        assert _derive_session_title([]) is None
        assert _derive_session_title([_user_msg("")]) is None
        assert _derive_session_title([_user_msg("   ")]) is None

    def test_no_user_message_falls_back_to_placeholder(self):
        """没有用户输入（新建未聊过）→ 返回 None，由调用方退到"新对话"。"""
        assert _derive_session_title([]) is None
        assert _derive_session_title([{"role": "assistant", "content": "你好"}]) is None

    def test_payload_as_json_string(self):
        """MySQL/OB 下 text() 裸 SQL 把 JSON 列以字符串返回，要能解码。"""
        payloads = [json.dumps(_user_msg("你好"))]
        assert _derive_session_title(payloads) == "你好"
