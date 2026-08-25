# -*- coding: utf-8 -*-
"""The ELLM chat model implementation (BOCOM adapter).

The BOCOM ELLM (Enterprise Large Language Model) gateway exposes an
OpenAI-compatible ``/chat/completions`` endpoint with two protocol
differences handled by this class:

- Streaming responses do not wrap their reasoning in ``<think>`` tags;
  when :attr:`inject_think_tag` is enabled the tag is prepended to the
  first non-empty text delta.
- The generation cap must be sent under the ``max_tokens`` field name
  (the adapter rejects ``max_completion_tokens``).

The key-refresh logic (api-key header rotation) lives in the
:class:`~bocomadp.middleware.ellm_refresh.EllmKeyRefreshMiddleware`,
which injects the fresh key per call via :meth:`set_api_key`; this class
only carries the protocol differences.

Model candidates (``list_models``) are read from the Redis hash
``bocomadp:model:think_tag`` — each field name is a model id and its
value (``"1"`` / ``"0"``) toggles ``<think>`` injection — instead of the
static ``_models/*.yaml`` files; the YAML directory remains as a
fallback when Redis is unreachable.
"""
import copy
import json
import logging
from collections import OrderedDict
from datetime import datetime
from typing import (
    Awaitable,
    Callable,
    Literal,
    Any,
    AsyncGenerator,
    TYPE_CHECKING,
    List,
    Type,
)

logger = logging.getLogger(__name__)

import redis
import redis.asyncio as aioredis

from pydantic import BaseModel, Field

from bocomadp.config import get_app_config

from agentscope.model._base import ChatModelBase, _TOOL_CHOICE_LITERAL_MODES
from agentscope.model._model_card import ModelCard
from agentscope.model._model_response import ChatResponse, StructuredResponse
from agentscope.model._model_usage import ChatUsage
from agentscope._utils._common import _generate_id
from agentscope.formatter import FormatterBase, DeepSeekChatFormatter
from agentscope.message import Msg, ThinkingBlock, ToolCallBlock, TextBlock
from agentscope.tool import ToolChoice

if TYPE_CHECKING:
    from bocomadp.credential import ELLMCredential
    from openai.types.chat import ChatCompletion
    from openai import AsyncStream
else:
    ChatCompletion = Any
    AsyncStream = Any


# ── 模型候选（Redis 真源，替代 _models/*.yaml） ────────────────
# ``list_models`` 从 Redis Hash ``bocomadp:model:think_tag`` 读取模型
# 列表：field 即模型名，value 为 JSON（见 :func:`_parse_model_meta`，兼容
# 旧格式 ``"0"`` / ``"1"``）。Redis 连接失败时降级读取本地
# _models/*.yaml（原逻辑），保证模型列表查询不因 Redis 抖动而不可用。
_MODEL_THINK_TAG_KEY = "bocomadp:model:think_tag"
# Redis 列表查询短超时（秒）：失败即降级 yaml，不阻塞凭证/表单渲染。
_REDIS_TIMEOUT = 1.0
# yaml 兜底默认值（与 providers/_models/*.yaml 一致）。
_DEFAULT_CONTEXT_SIZE = 1000000
_DEFAULT_OUTPUT_SIZE = 384000
# 构造回退值：Redis 无该模型记录/不可用时，context_size 保持原默认。
_FALLBACK_CONTEXT_SIZE = 65536


def _get_model_context_size(model: str) -> int:
    """按模型名从 Redis 读取 context_size；无记录/不可用回退默认值。

    Args:
        model (`str`): 模型名（Redis Hash ``bocomadp:model:think_tag`` 的 field）。

    Returns:
        `int`: 该模型的 context_size；Redis 无记录/查询失败时回退
        ``_FALLBACK_CONTEXT_SIZE``（65536，保持原构造默认）。
    """
    try:
        cfg = get_app_config().redis
        client = redis.Redis(
            host=cfg.host,
            port=cfg.port,
            socket_connect_timeout=_REDIS_TIMEOUT,
            socket_timeout=_REDIS_TIMEOUT,
        )
        raw = client.hget(_MODEL_THINK_TAG_KEY, model)
    except Exception as e:  # pragma: no cover - Redis 不可用
        logger.warning(
            "EllmChatModel: Redis read failed for context_size of "
            "model %r; fallback to %d: %s",
            model,
            _FALLBACK_CONTEXT_SIZE,
            e,
        )
        return _FALLBACK_CONTEXT_SIZE
    if raw is None:
        return _FALLBACK_CONTEXT_SIZE
    _, context_size, _ = _parse_model_meta(raw)
    return context_size


async def _get_think_tag_from_redis(model: str) -> bool:
    """按模型名从 Redis 读取 ``inject_think_tag``（供中间件异步调用）。

    Args:
        model (`str`): 模型名（Redis Hash ``bocomadp:model:think_tag`` 的 field）。

    Returns:
        `bool`: 该模型的 think_tag；Redis 无该模型记录/连接失败时返回
        ``False``（安全默认，不误加 ``<think>`` 前缀）。
    """
    try:
        cfg = get_app_config().redis
        client = aioredis.Redis(
            host=cfg.host,
            port=cfg.port,
            socket_connect_timeout=_REDIS_TIMEOUT,
            socket_timeout=_REDIS_TIMEOUT,
        )
        try:
            raw = await client.hget(_MODEL_THINK_TAG_KEY, model)
        finally:
            await client.aclose()
    except Exception as e:  # pragma: no cover - Redis 不可用
        logger.warning(
            "EllmChatModel: Redis read failed for inject_think_tag of "
            "model %r; fallback to False: %s",
            model,
            e,
        )
        return False
    if raw is None:
        return False
    think, _, _ = _parse_model_meta(raw)
    return think


def _parse_model_meta(value: Any) -> tuple[bool, int, int]:
    """解析 Redis value 为 ``(think_tag, context_size, output_size)``。

    仅接受 JSON 格式：``{"think_tag": 1, "context_size": 1000000, \
"output_size": 384000}``；非 JSON 或字段缺失时回退默认值
    （think_tag=False，context_size / output_size 取默认值）。
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            think = data.get("think_tag") in (1, True)
            context_size = int(
                data.get("context_size") or _DEFAULT_CONTEXT_SIZE
            )
            output_size = int(
                data.get("output_size") or _DEFAULT_OUTPUT_SIZE
            )
            return think, context_size, output_size
    except (ValueError, TypeError):
        pass
    return False, _DEFAULT_CONTEXT_SIZE, _DEFAULT_OUTPUT_SIZE


def _build_parameter_schema(
    output_types: list[str],
    output_size: int,
) -> dict[str, Any]:
    """构造 ModelCard.parameter_schema（对齐 ``ModelCard.from_yaml`` 合并逻辑）。

    - 不支持 thinking 输出（think_tag="0"）时剔除 thinking 参数；
    - ``max_tokens.maximum`` 由 ``output_size`` 决定。
    """
    base_schema = EllmChatModel.Parameters.model_json_schema()
    properties = copy.deepcopy(base_schema.get("properties", {}))
    if "application/x-thinking" not in output_types:
        properties.pop("thinking_enable", None)
        properties.pop("thinking_budget", None)
    if "max_tokens" in properties:
        properties["max_tokens"]["maximum"] = output_size
    return {
        "type": "object",
        "properties": properties,
        "required": base_schema.get("required", []),
    }


def _list_models_from_redis() -> list[ModelCard] | None:
    """从 Redis 读取模型候选列表。

    Returns:
        `list[ModelCard] | None`: 读取成功返回卡片列表（可为空列表）；
        Redis 连接失败返回 ``None``，由调用方降级读取 yaml。
    """
    try:
        cfg = get_app_config().redis
        client = redis.Redis(
            host=cfg.host,
            port=cfg.port,
            socket_connect_timeout=_REDIS_TIMEOUT,
            socket_timeout=_REDIS_TIMEOUT,
        )
        mapping = client.hgetall(_MODEL_THINK_TAG_KEY)
    except Exception as e:  # pragma: no cover - Redis 不可用
        logger.warning(
            "EllmChatModel.list_models: Redis read failed, "
            "fallback to _models yaml: %s",
            e,
        )
        return None

    cards: list[ModelCard] = []
    for raw_name, raw_tag in (mapping or {}).items():
        name = (
            raw_name.decode("utf-8", "replace")
            if isinstance(raw_name, bytes)
            else str(raw_name)
        )
        think, context_size, output_size = _parse_model_meta(raw_tag)
        output_types = ["text/plain"]
        if think:
            output_types.append("application/x-thinking")
        cards.append(
            ModelCard(
                name=name,
                label=name,
                status="active",
                input_types=["text/plain"],
                output_types=output_types,
                context_size=context_size,
                output_size=output_size,
                parameter_schema=_build_parameter_schema(
                    output_types,
                    output_size,
                ),
                parameters_overrides={},
            ),
        )
    return cards


class EllmChatModel(ChatModelBase):
    """The ELLM chat model."""

    type: Literal["ellm_chat"] = "ellm_chat"
    """The type of the chat model."""

    inject_think_tag: bool = False
    """Whether to prepend a ``<think>`` tag to the first non-empty text
    delta of streaming responses. The base class ``__init__`` does not
    accept this argument, so it is toggled as an instance attribute
    (``model.inject_think_tag = True``)."""

    class Parameters(BaseModel):
        """The parameters for the ELLM chat model."""

        max_tokens: int | None = Field(
            default=None,
            title="Max Tokens",
            description="The maximum number of tokens for the LLM output.",
            gt=0,
        )

        temperature: float | None = Field(
            default=None,
            title="Temperature",
            description="The temperature for the LLM output.",
            ge=0,
            le=2,
        )

        top_p: float | None = Field(
            default=None,
            title="Top P",
            description="The top P value for the LLM output.",
            gt=0,
            le=1,
        )

    def __init__(
        self,
        credential: "ELLMCredential",
        model: str,
        parameters: "EllmChatModel.Parameters | None" = None,
        stream: bool = True,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        context_size: int | None = None,
        formatter: FormatterBase | None = None,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the ELLM chat model.

        Args:
            credential (`ELLMCredential`):
                The BOCOM ELLM credential used to authenticate API calls.
            model (`str`):
                The ELLM model name, e.g. ``Qwen3-235B-A22B``.
            parameters (`EllmChatModel.Parameters | None`, defaults to \
            `None`):
                The ELLM API parameters. When ``None``, the default
                parameters will be used.
            stream (`bool`, defaults to `True`):
                Whether to enable streaming output.
            max_retries (`int`, defaults to `3`):
                The maximum number of retries for the ELLM API.
            retry_delay (`float`, defaults to `1.0`):
                Seconds to sleep between retry attempts.
            context_size (`int | None`, defaults to `None`):
                The model context size used for context compression.
                ``None`` 时按 ``model`` 从 Redis（``bocomadp:model:think_tag``）
                读取；Redis 无该模型记录或不可用时回退 65536。
            formatter (`FormatterBase | None`, defaults to `None`):
                The formatter that converts ``Msg`` objects to the format
                required by the ELLM API. When ``None``, a
                ``DeepSeekChatFormatter`` instance will be used.
            client_kwargs (`dict[str, Any] | None`, defaults to `None`):
                Extra keyword arguments forwarded to ``openai.AsyncClient``
                (e.g. ``timeout``, ``default_headers``, ``http_client``).
        """
        # context_size 未显式指定时按模型名从 Redis 读取（覆盖默认值）
        if context_size is None:
            context_size = _get_model_context_size(model)
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters or self.Parameters(),
            stream=stream,
            max_retries=max_retries,
            retry_delay=retry_delay,
            context_size=context_size,
        )
        self.formatter = formatter or DeepSeekChatFormatter()
        self.client_kwargs = client_kwargs or {}
        # Request-level api key override, injected per call by the
        # ``EllmKeyRefreshMiddleware``.  When unset, the static
        # ``credential.api_key`` configured at construction time is used.
        self._api_key_override: str | None = None
        # Callback invoked when a 401-triggered forced key refresh fails:
        # it forcibly marks the credential's stored key as expired so the
        # *next* conversation using this credential refreshes it lazily.
        # Injected per call by the ``EllmKeyRefreshMiddleware`` (bound to
        # the current credential); when ``None`` (e.g. the model is called
        # outside the middleware) a failed refresh is surfaced as-is.
        self._auth_invalidate_callback: (
            Callable[[], Awaitable[None]] | None
        ) = None

        # Callback invoked when the gateway rejects our key with an
        # ``invalid_api_key`` 401.  It must force-fetch a fresh key
        # (lock-protected) and return it so the current call can be
        # retried once.  Injected per call by the ``EllmKeyRefreshMiddleware``;
        # when ``None`` (e.g. the model is called outside the middleware) a
        # 401 is surfaced as-is without refreshing/retrying.
        self._refresh_key_callback: (
            Callable[[], Awaitable[str]] | None
        ) = None

        import openai

        self.client: openai.AsyncClient = openai.AsyncClient(
            api_key=self.credential.api_key.get_secret_value(),
            base_url=self.credential.base_url,
            **self.client_kwargs,
        )

    def set_api_key(self, api_key: str) -> None:
        """Set the request-level API key override for subsequent calls.

        Used by :class:`~bocomadp.middleware.ellm_refresh.
        EllmKeyRefreshMiddleware` to inject a freshly-refreshed key before
        each model call without recreating the client.  When unset, the
        static ``credential.api_key`` configured at construction time is
        used.

        Args:
            api_key (`str`):
                The API key to send as ``Authorization: Bearer <api_key>``.
        """
        self._api_key_override = api_key

    def set_auth_invalidate_callback(
        self,
        callback: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Set (or clear) the refresh-failure invalidation callback.

        The callback is invoked when a 401-triggered forced key refresh
        fails; it should forcibly expire the underlying credential so the
        next call using it refreshes the key.  It is injected per call by
        the
        :class:`~bocomadp.middleware.ellm_refresh.EllmKeyRefreshMiddleware`
        and is bound to the credential actually in use for this call.

        Args:
            callback (`Callable[[], Awaitable[None]] | None`):
                Async no-arg callback, or ``None`` to disable invalidation.
        """
        self._auth_invalidate_callback = callback

    def set_refresh_key_callback(
        self,
        callback: Callable[[], Awaitable[str]] | None,
    ) -> None:
        """Set (or clear) the forced key-refresh callback.

        The callback is invoked when the gateway rejects our key with an
        ``invalid_api_key`` 401: it must force-fetch a fresh key
        (lock-protected) and return it, so the current call can be retried
        once with the new key.  It is injected per call by the
        :class:`~bocomadp.middleware.ellm_refresh.EllmKeyRefreshMiddleware`
        and is bound to the credential actually in use for this call.

        Args:
            callback (`Callable[[], Awaitable[str]] | None`):
                Async no-arg callback returning a fresh key, or ``None`` to
                disable refresh-and-retry on 401.
        """
        self._refresh_key_callback = callback

    async def aclose(self) -> None:
        """Close the underlying openai client and release its connection pool.

        Used by the summarization middleware after a temporary
        compression-model instance is done (see
        ``bocomadp.middleware.summarization``); session-scoped models
        are recreated per run and do not need this.
        """
        await self.client.close()

    @staticmethod
    def _is_invalid_key_error(exc: Any) -> bool:
        """Whether an exception is a "key missing/expired" 401.

        Different upstream gateways report an invalid/expired API key with
        different payloads, all over HTTP 401:

        - BOCOM ELLM gateway: ``{"error": {"code": "invalid_api_key",
          "message": "API KEY不存在或已过期"}}``;
        - OpenAI / DeepSeek official: ``{"error": {"type":
          "authentication_error", "code": "invalid_request_error",
          "message": "Authentication Fails, Your api key: **** is invalid"}}``.

        We accept any of: ``code == "invalid_api_key"``, ``type ==
        "authentication_error"``, or message keywords (``不存在或已过期`` /
        ``invalid`` / ``authentication``) so a 401 that really means "the key
        is bad" triggers refresh-and-retry across gateways.
        """
        if getattr(exc, "status_code", None) != 401:
            return False
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            code = str(body.get("code") or "")
            etype = str(body.get("type") or "")
            message = str(body.get("message") or "")
            if code == "invalid_api_key":
                return True
            if etype == "authentication_error":
                return True
            lower = message.lower()
            if "不存在或已过期" in message:
                return True
            if "invalid" in lower or "authentication" in lower:
                return True
        # Fallback: streamed 401 leaves ``body`` empty — scan the exception
        # text instead (e.g. "... Your api key: **** is invalid ...").
        exc_text = str(exc)
        lower = exc_text.lower()
        if "invalid" in lower or "authentication" in lower or "api key" in lower:
            return True
        return False

    @classmethod
    def _get_retryable_exceptions(cls) -> tuple[Type[Exception], ...]:
        import openai

        return (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

    @classmethod
    def list_models(
        cls,
        custom_yaml_dir: str | None = None,
    ) -> list[ModelCard]:
        """候选模型：优先 Redis（``bocomadp:model:think_tag``），失败降级 yaml。

        覆盖基类 :meth:`ChatModelBase.list_models`。Redis Hash 的 field 即
        模型名，值 ``"1"`` 启用 <think> 注入（output_types 含
        ``application/x-thinking``），``"0"`` 不启用。

        Args:
            custom_yaml_dir (`str | None`): 降级 yaml 时使用的目录。

        Returns:
            `list[ModelCard]`: 模型候选卡片列表。
        """
        cards = _list_models_from_redis()
        if cards is not None:
            return cards
        return super().list_models(custom_yaml_dir)

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **generate_kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """Call the ELLM chat completions API.

        Args:
            model_name (`str`):
                The model name to use for this call.
            messages (`list`):
                A list of message dicts with ``role`` and ``content`` keys.
            tools (`list[dict]`, default `None`):
                The tools JSON schemas.
            tool_choice (`ToolChoice | None`, optional):
                Controls which (if any) tool is called by the model.
            **generate_kwargs (`Any`):
                Extra keyword arguments forwarded to the API.

        Returns:
            `ChatResponse | AsyncGenerator[ChatResponse, None]`:
                A ``ChatResponse`` when streaming is disabled, or an async
                generator of ``ChatResponse`` objects when streaming is
                enabled.
        """
        formatted_messages = await self.formatter.format(messages)

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
            "stream": self.stream,
        }

        if self.parameters.max_tokens is not None:
            kwargs["max_tokens"] = self.parameters.max_tokens

        if self.parameters.temperature is not None:
            kwargs["temperature"] = self.parameters.temperature

        if self.parameters.top_p is not None:
            kwargs["top_p"] = self.parameters.top_p

        kwargs.update(generate_kwargs)

        if self._api_key_override:
            headers = dict(kwargs.pop("extra_headers", None) or {})
            headers["Authorization"] = f"Bearer {self._api_key_override}"
            kwargs["extra_headers"] = headers

        fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)

        if fmt_tools:
            kwargs["tools"] = fmt_tools

        if fmt_tool_choice is not None:
            kwargs["tool_choice"] = fmt_tool_choice

        start_datetime = datetime.now()
        response = await self._request_with_retry_on_auth(kwargs)
        if self.stream:
            return self._parse_stream_response(start_datetime, response)

        return self._parse_completion_response(start_datetime, response)

    async def _request_with_retry_on_auth(
        self,
        kwargs: dict[str, Any],
    ) -> Any:
        """Send the request; on a ``invalid_api_key`` 401, force-refresh the
        key and retry once.

        Refresh/retry only happens when a refresh callback is installed
        (middleware).  If the forced refresh fails to yield a new key, the
        credential is marked as expired (via ``_auth_invalidate_callback``)
        so the *next* call refreshes lazily, and the original 401 is
        surfaced.
        """
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not self._is_invalid_key_error(exc):
                raise

            if self._refresh_key_callback is None:
                # No refresh capability (model called outside middleware) —
                # surface the 401 as-is.
                logger.warning(
                    "ELLM 401 with no refresh callback installed; "
                    "key not refreshed",
                )
                raise

            try:
                new_key = await self._refresh_key_callback()
            except Exception as refresh_exc:  # noqa: BLE001
                await self._mark_refresh_failed(refresh_exc)
                raise

            # Only retry once when a genuinely new key was obtained; a
            # fallback to the old key would just 401 again.
            if new_key and new_key != self._api_key_override:
                self._api_key_override = new_key
                headers = dict(kwargs.pop("extra_headers", None) or {})
                headers["Authorization"] = f"Bearer {new_key}"
                kwargs["extra_headers"] = headers
                logger.info(
                    "ELLM 401: refreshed key and retrying once "
                    "(credential=%s)",
                    getattr(self.credential, "id", None),
                )
                return await self.client.chat.completions.create(**kwargs)

            # Refresh did not yield a new key — mark expired and surface.
            await self._mark_refresh_failed(None)
            raise

    async def _mark_refresh_failed(self, cause: BaseException | None) -> None:
        """Best-effort mark the credential's key as expired after a failed
        forced refresh, so the next call refreshes it lazily.  Failures are
        swallowed with a warning."""
        if self._auth_invalidate_callback is None:
            return
        try:
            await self._auth_invalidate_callback()
            logger.info(
                "ELLM key invalidated after failed 401 refresh "
                "(credential=%s, cause=%s)",
                getattr(self.credential, "id", None),
                cause,
            )
        except Exception as invalidate_exc:  # noqa: BLE001
            logger.warning(
                "ELLM key invalidation failed after 401 "
                "(error=%s)",
                invalidate_exc,
            )

    async def _parse_stream_response(
        self,
        start_datetime: datetime,
        response: AsyncStream,
    ) -> AsyncGenerator[ChatResponse, None]:
        """Parse the ELLM streaming response.

        When :attr:`inject_think_tag` is enabled, a ``<think>`` tag is
        prepended to the first non-empty text delta (empty deltas are
        skipped, so the tag always lands on real content).

        Args:
            start_datetime (`datetime`):
                The start datetime of the response generation.
            response (`AsyncStream`):
                The OpenAI-compatible async stream object.

        Yields:
            `ChatResponse`:
                Incremental ``ChatResponse`` objects with ``is_last=False``
                followed by a final one with ``is_last=True``.
        """
        usage = None
        response_id: str = _generate_id()
        text_id: str = _generate_id()
        thinking_id: str = _generate_id()
        # The mapping from index to tool call id
        tool_call_mapping: dict = OrderedDict()
        # Whether the <think> tag has been injected into the first
        # non-empty text delta
        _think_injected = False

        async with response as stream:
            async for chunk in stream:
                delta_res = ChatResponse(
                    content=[],
                    is_last=False,
                    id=response_id,
                )

                # Update the response ID if exists
                response_id = getattr(chunk, "id", None) or response_id
                delta_res.id = response_id

                if chunk.usage:
                    u = chunk.usage
                    usage = ChatUsage(
                        input_tokens=u.prompt_tokens,
                        output_tokens=u.completion_tokens,
                        time=(datetime.now() - start_datetime).total_seconds(),
                        cache_input_tokens=getattr(
                            u,
                            "prompt_cache_hit_tokens",
                            0,
                        ),
                    )

                if not chunk.choices:
                    # The gateway may emit a trailing usage-only chunk with
                    # no choices; forward it as an empty-content delta so
                    # the base class ``__call__`` can absorb ``usage`` into
                    # ``acc_res``. The empty delta itself is filtered out
                    # of the surfaced stream by ``_stream``.
                    if usage is not None:
                        delta_res.usage = usage
                        yield delta_res
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # Thinking block
                if getattr(delta, "reasoning_content", None):
                    delta_res.append_thinking(
                        block_id=thinking_id,
                        thinking=delta.reasoning_content,
                    )

                # Text
                if getattr(delta, "content", None):
                    delta_text = delta.content
                    if self.inject_think_tag and not _think_injected:
                        delta_text = "<think>" + delta_text
                        _think_injected = True
                    delta_res.append_text(
                        block_id=text_id,
                        text=delta_text,
                    )

                # Tool call
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    index = tool_call.index
                    fn = getattr(tool_call, "function", None)
                    delta_name = getattr(fn, "name", None) if fn else None
                    delta_args = getattr(fn, "arguments", None) if fn else None

                    # Record the id and name in case following deltas
                    # don't provide them
                    if index not in tool_call_mapping:
                        tool_call_mapping[index] = (
                            tool_call.id,
                            delta_name or "unknown",
                        )

                    stored_id, stored_name = tool_call_mapping[index]

                    delta_res.append_tool_call(
                        block_id=tool_call.id or stored_id,
                        name=delta_name or stored_name,
                        input=delta_args or "",
                    )

                if delta_res.content or usage:
                    delta_res.usage = usage
                    yield delta_res

    def _parse_completion_response(
        self,
        start_datetime: datetime,
        response: ChatCompletion,
    ) -> ChatResponse:
        """Parse the ELLM non-streaming response.

        Args:
            start_datetime (`datetime`):
                The start datetime of the response generation.
            response (`ChatCompletion`):
                The OpenAI-compatible chat completion object.

        Returns:
            `ChatResponse`:
                A single ``ChatResponse`` with ``is_last=True``.
        """
        content_blocks: List[TextBlock | ToolCallBlock | ThinkingBlock] = []

        if response.choices:
            choice = response.choices[0]
            reasoning = getattr(choice.message, "reasoning_content", None)
            if isinstance(reasoning, str) and reasoning:
                content_blocks.append(ThinkingBlock(thinking=reasoning))

            if choice.message.content:
                content_blocks.append(TextBlock(text=choice.message.content))

            for tool_call in choice.message.tool_calls or []:
                content_blocks.append(
                    ToolCallBlock(
                        id=tool_call.id,
                        name=tool_call.function.name,
                        input=tool_call.function.arguments,
                    ),
                )

        usage = None
        if response.usage:
            u = response.usage
            usage = ChatUsage(
                input_tokens=u.prompt_tokens,
                output_tokens=u.completion_tokens,
                time=(datetime.now() - start_datetime).total_seconds(),
                cache_input_tokens=getattr(
                    u,
                    "prompt_cache_hit_tokens",
                    0,
                ),
            )

        resp_kwargs: dict[str, Any] = {
            "content": content_blocks,
            "is_last": True,
            "usage": usage,
        }
        response_id = getattr(response, "id", None)
        if response_id:
            resp_kwargs["id"] = response_id

        return ChatResponse(**resp_kwargs)

    def _format_tools(
        self,
        tools: list[dict] | None,
        tool_choice: ToolChoice | None,
    ) -> tuple[list[dict] | None, str | dict | None]:
        """Validate, filter, and format tools and tool_choice for the ELLM
        API.

        When ``tool_choice.tools`` is specified the schemas list is filtered
        to only those tools. When ``tool_choice.mode`` is a specific tool name
        (str) the model is forced to call exactly that tool without needing to
        filter the list, preserving prompt-cache efficiency.

        Args:
            tools (`list[dict] | None`, optional):
                The raw tool schemas.
            tool_choice (`ToolChoice | None`, optional):
                The tool choice configuration.

        Returns:
            `tuple[list[dict] | None, str | dict | None]`:
                A tuple of (formatted_tools, formatted_tool_choice).
        """
        if tool_choice and tools:
            self._validate_tool_choice(tool_choice, tools)
            if tool_choice.tools:
                allowed = set(tool_choice.tools)
                tools = [t for t in tools if t["function"]["name"] in allowed]

        if not tool_choice:
            return tools, None

        mode = tool_choice.mode

        if mode not in _TOOL_CHOICE_LITERAL_MODES:
            return tools, {"type": "function", "function": {"name": mode}}

        return tools, mode


__all__ = ["EllmChatModel"]
