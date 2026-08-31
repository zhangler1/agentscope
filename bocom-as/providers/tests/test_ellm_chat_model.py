# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for the BOCOM :class:`EllmChatModel` with mocked API
responses (migrated from the core ``tests/model_ellm_test.py``).

The BOCOM ELLM gateway mirrors the OpenAI chat-completions protocol but
differs in two ways, both covered here:

- Streaming think-tag injection: when ``inject_think_tag`` is enabled (set
  via instance attribute, the base class ``__init__`` does not accept it)
  the ``<think>`` tag is prepended to the first non-empty text delta.
  Empty deltas are skipped so the tag always lands on real content.
- ``max_tokens`` is forwarded under its native field name (never
  ``max_completion_tokens``) and omitted entirely when ``None``.
"""
from typing import Any
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from providers.credential import ELLMCredential
from providers.ellm_chat_model import EllmChatModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_model(stream: bool = False, **kwargs: Any) -> Any:
    return EllmChatModel(
        credential=ELLMCredential(
            api_key="test",
            base_url="http://localhost",
            model="Qwen3-235B-A22B",
        ),
        model="Qwen3-235B-A22B",
        stream=stream,
        context_size=128_000,
        **kwargs,
    )


def _mock_completion(
    text: Any = None,
    response_id: str = "ellm-1",
) -> MagicMock:
    """Build a mock non-streaming ChatCompletion response."""
    msg = MagicMock()
    msg.content = text
    msg.reasoning_content = None
    msg.tool_calls = None

    choice = MagicMock()
    choice.message = msg

    resp = MagicMock()
    resp.id = response_id
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    return resp


def _make_stream_chunk(
    delta_text: str | None = None,
    response_id: str = "ellm-1",
    usage: dict | None = None,
    has_choices: bool = True,
) -> MagicMock:
    """Build a single mock streaming chunk."""
    chunk = MagicMock()
    chunk.id = response_id

    if usage:
        chunk.usage = MagicMock()
        chunk.usage.prompt_tokens = usage.get("prompt_tokens", 0)
        chunk.usage.completion_tokens = usage.get("completion_tokens", 0)
        chunk.usage.prompt_cache_hit_tokens = 0
    else:
        chunk.usage = None

    if has_choices:
        delta = MagicMock()
        delta.content = delta_text
        delta.reasoning_content = None
        delta.tool_calls = None
        choice = MagicMock()
        choice.delta = delta
        chunk.choices = [choice]
    else:
        chunk.choices = []

    return chunk


class _MockAsyncStream:
    """Mock async stream that acts as an async context manager + iterator."""

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks
        self._index = 0

    async def __aenter__(self) -> "_MockAsyncStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> "_MockAsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


# ---------------------------------------------------------------------------
# Streaming think-tag injection
# ---------------------------------------------------------------------------


class TestEllmThinkTagInjection(IsolatedAsyncioTestCase):
    """Tests for streaming ``<think>`` tag injection."""

    def setUp(self) -> None:
        self.model = _make_model(stream=True)
        self.model.inject_think_tag = True
        # Client is built eagerly in __init__; inject a mock onto the
        # instance so create() hits it instead of the network.
        self.mock_client = MagicMock()
        self.model.client = self.mock_client

    async def test_think_tag_injected_on_first_text_chunk(self) -> None:
        """The first non-empty text delta is prefixed with ``<think>``."""
        chunks = [
            _make_stream_chunk(delta_text="Hello"),
            _make_stream_chunk(delta_text=" world"),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertTrue(
            responses[0].content[0].text.startswith("<think>"),
        )
        self.assertEqual(responses[0].content[0].text, "<think>Hello")
        # Subsequent deltas are untouched.
        self.assertEqual(responses[1].content[0].text, " world")
        # The final accumulated chunk carries the tag only once.
        self.assertEqual(
            responses[-1].content[0].text,
            "<think>Hello world",
        )

    async def test_think_tag_not_injected_when_disabled(self) -> None:
        """With the default ``inject_think_tag=False`` no tag is added."""
        self.model.inject_think_tag = False
        chunks = [
            _make_stream_chunk(delta_text="Hello"),
            _make_stream_chunk(delta_text=" world"),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        self.assertEqual(responses[0].content[0].text, "Hello")
        self.assertEqual(responses[-1].content[0].text, "Hello world")

    async def test_think_tag_skips_empty_delta(self) -> None:
        """The tag lands on the first *non-empty* text delta."""
        chunks = [
            _make_stream_chunk(delta_text=""),
            _make_stream_chunk(delta_text="Hello"),
            _make_stream_chunk(delta_text=" world"),
            _make_stream_chunk(
                has_choices=False,
                usage={"prompt_tokens": 10, "completion_tokens": 3},
            ),
        ]
        mock_create = AsyncMock(return_value=_MockAsyncStream(chunks))
        self.mock_client.chat.completions.create = mock_create

        gen = await self.model([])
        responses = [r async for r in gen]

        # The empty delta is absorbed by the base ``__call__`` wrapper and
        # never surfaced, so the first emitted delta carries the tag.
        self.assertEqual(responses[0].content[0].text, "<think>Hello")


# ---------------------------------------------------------------------------
# max_tokens field name
# ---------------------------------------------------------------------------


class TestEllmMaxTokensField(IsolatedAsyncioTestCase):
    """Tests for the ``max_tokens`` request field name."""

    def setUp(self) -> None:
        self.mock_client = MagicMock()
        self.mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_completion(text="Hello"),
        )

    async def test_max_tokens_sent_as_max_tokens_field(self) -> None:
        """``max_tokens`` is sent under its native name, never
        ``max_completion_tokens``."""
        model = _make_model(
            stream=False,
            parameters=EllmChatModel.Parameters(max_tokens=100),
        )
        model.client = self.mock_client

        await model([])

        kwargs = self.mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["max_tokens"], 100)
        self.assertNotIn("max_completion_tokens", kwargs)

    async def test_max_tokens_none_omitted(self) -> None:
        """When ``parameters.max_tokens`` is ``None`` neither token field is
        sent."""
        model = _make_model(
            stream=False,
            parameters=EllmChatModel.Parameters(),
        )
        model.client = self.mock_client

        await model([])

        kwargs = self.mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("max_completion_tokens", kwargs)


# ---------------------------------------------------------------------------
# Request-level api key override (set_api_key)
# ---------------------------------------------------------------------------


class TestSetApiKey(IsolatedAsyncioTestCase):
    """The request-level api key override injected by the middleware."""

    def setUp(self) -> None:
        self.mock_client = MagicMock()
        self.mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_completion(text="Hello"),
        )

    async def test_set_api_key_override_is_sent_as_bearer(self) -> None:
        """After ``set_api_key`` every call sends
        ``Authorization: Bearer <override>`` (request-level, client kept)."""
        model = _make_model(stream=False)
        model.client = self.mock_client
        model.set_api_key("refreshed-key")

        await model([])

        kwargs = self.mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(
            kwargs["extra_headers"],
            {"Authorization": "Bearer refreshed-key"},
        )

    async def test_unset_override_sends_no_extra_headers(self) -> None:
        """Without ``set_api_key`` no extra_headers are injected — the
        client's static api key (credential) is used as-is."""
        model = _make_model(stream=False)
        model.client = self.mock_client

        await model([])

        kwargs = self.mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("extra_headers", kwargs)

    async def test_override_wins_over_explicit_extra_headers(self) -> None:
        """The refresh override takes precedence over caller-passed
        extra_headers (key rotation is the hard requirement)."""
        model = _make_model(stream=False)
        model.client = self.mock_client
        model.set_api_key("refreshed-key")

        await model([], extra_headers={"X-Custom": "1"})

        kwargs = self.mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(
            kwargs["extra_headers"],
            {"X-Custom": "1", "Authorization": "Bearer refreshed-key"},
        )


# ---------------------------------------------------------------------------
# 401 → forced key refresh → retry once
# ---------------------------------------------------------------------------


class _Fake401(Exception):
    """A stand-in for the gateway's ``invalid_api_key`` 401 error."""

    status_code = 401

    def __init__(self) -> None:
        super().__init__("invalid_api_key")
        self.body = {
            "error": {
                "code": "invalid_api_key",
                "message": "API KEY不存在或已过期",
            },
        }


class _Fake401DeepSeek(Exception):
    """A DeepSeek/official-OpenAI-style 401 with ``authentication_error``."""

    status_code = 401

    def __init__(self) -> None:
        super().__init__("Authentication Fails")
        self.body = {
            "error": {
                "message": "Authentication Fails, Your api key: ****1111 is "
                "invalid",
                "type": "authentication_error",
                "param": None,
                "code": "invalid_request_error",
            },
        }


class TestCallApi401Refresh(IsolatedAsyncioTestCase):
    """``_request_with_retry_on_auth`` force-refreshes the key and retries
    once on an ``invalid_api_key`` 401."""

    def setUp(self) -> None:
        self.mock_client = MagicMock()
        self.mock_client.chat.completions.create = AsyncMock(
            return_value=_mock_completion(text="Hello"),
        )
        self.model = _make_model(stream=False)
        self.model.client = self.mock_client

    async def test_401_refreshes_and_retries_once(self) -> None:
        """A 401 triggers a forced refresh; the call is retried once with the
        new key and succeeds.  The invalidation callback is not invoked
        because the refresh succeeded."""
        create = self.mock_client.chat.completions.create
        create.side_effect = [_Fake401(), _mock_completion(text="Hello")]

        self.model.set_api_key("old-key")
        self.model.set_refresh_key_callback(AsyncMock(return_value="new-key"))
        invalidated = AsyncMock()
        self.model.set_auth_invalidate_callback(invalidated)

        resp = await self.model._request_with_retry_on_auth(
            {"model": "Qwen3-235B-A22B", "stream": False},
        )

        self.assertEqual(create.await_count, 2)
        # The retried request carries the freshly refreshed key.
        retry_kwargs = create.await_args.kwargs
        self.assertEqual(
            retry_kwargs["extra_headers"],
            {"Authorization": "Bearer new-key"},
        )
        # The retry succeeded and returned the completion from the 2nd call.
        self.assertEqual(resp.choices[0].message.content, "Hello")
        invalidated.assert_not_awaited()

    async def test_401_refresh_failure_invalidates_and_raises(self) -> None:
        """When the forced refresh does not yield a new key, the call is not
        retried, the credential is marked expired, and the 401 surfaces."""
        create = self.mock_client.chat.completions.create
        create.side_effect = _Fake401()  # keep failing

        self.model.set_api_key("old-key")
        # Refresh "fails": returns None (no new key).
        self.model.set_refresh_key_callback(AsyncMock(return_value=None))
        invalidated = AsyncMock()
        self.model.set_auth_invalidate_callback(invalidated)

        with self.assertRaises(_Fake401):
            await self.model._request_with_retry_on_auth(
                {"model": "Qwen3-235B-A22B", "stream": False},
            )

        self.assertEqual(create.await_count, 1)  # no retry
        invalidated.assert_awaited_once()

    async def test_401_without_refresh_callback_raises(self) -> None:
        """Without a refresh callback (model called outside middleware), a
        401 is surfaced as-is without retrying."""
        create = self.mock_client.chat.completions.create
        create.side_effect = _Fake401()

        invalidated = AsyncMock()
        self.model.set_auth_invalidate_callback(invalidated)

        with self.assertRaises(_Fake401):
            await self.model._request_with_retry_on_auth(
                {"model": "Qwen3-235B-A22B", "stream": False},
            )

        self.assertEqual(create.await_count, 1)
        invalidated.assert_not_awaited()

    async def test_deepseek_auth_error_401_also_retries(self) -> None:
        """A DeepSeek/official-OpenAI 401 (``type: authentication_error``)
        is also recognised as an invalid-key error and triggers refresh +
        retry once."""
        create = self.mock_client.chat.completions.create
        create.side_effect = [_Fake401DeepSeek(), _mock_completion(text="OK")]

        self.model.set_api_key("old-key")
        self.model.set_refresh_key_callback(AsyncMock(return_value="new-key"))
        invalidated = AsyncMock()
        self.model.set_auth_invalidate_callback(invalidated)

        resp = await self.model._request_with_retry_on_auth(
            {"model": "Qwen3-235B-A22B", "stream": False},
        )

        self.assertEqual(create.await_count, 2)
        retry_kwargs = create.await_args.kwargs
        self.assertEqual(
            retry_kwargs["extra_headers"],
            {"Authorization": "Bearer new-key"},
        )
        self.assertEqual(resp.choices[0].message.content, "OK")
        invalidated.assert_not_awaited()


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


class TestAclose(IsolatedAsyncioTestCase):
    """``aclose`` 关闭底层 openai client，释放连接池。"""

    async def test_aclose_closes_client(self) -> None:
        model = _make_model(stream=False)
        model.client = MagicMock()
        model.client.close = AsyncMock()

        await model.aclose()

        model.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
