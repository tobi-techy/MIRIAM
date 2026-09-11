"""Concentrate AI provider for Miriam Financial Agent.

Talk to the Concentrate Responses API (``POST {base}/responses``) which
routes across OpenAI / Anthropic / Gemini with automatic model fallbacks,
streaming tool calls, and prompt caching. The provider keeps the shared
``LLMProvider`` interface so the agent loop stays provider-agnostic.

Documented behaviors this provider relies on (https://concentrate.ai/docs):
  - Body uses ``input`` (string or array of message/tool items), NOT OpenAI
    ``messages``; tool definitions are flat (not the nested OpenAI format).
  - Max output is set with ``max_output_tokens`` (not ``max_tokens``).
  - Multi-turn tool calls are paired via ``function_call`` /
    ``function_call_output`` items with a matching ``call_id``.
  - Streaming returns SSE ``data:`` frames; payloads carry a ``type`` field
    and may nest values under ``data`` (the docs are inconsistent, so this
    parser reads both shapes defensively).
  - ``routing.provider.sort`` + ``routing.model.fallbacks`` drive failover.
  - OpenAI-family prompt caching via top-level ``prompt_cache_options``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from miriam_agent.agents.llm import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    trim_messages,
)
from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import (
    AgentError,
    AuthenticationError,
    ConfigurationError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

RESPONSES_PATH = "/responses"

# Status codes worth retrying after transient provider/network conditions.
RETRYABLE_STATUS = {408, 424, 429, 500, 502, 503, 504}

# Fallback per-1k-token prices used by the cost guard (provider slugs may
# be prefixed, e.g. "openai/gpt-5.6-terra"; we normalize before lookup).
DEFAULT_COST_PER_1K = {"prompt": 0.0010, "completion": 0.0040}
COST_PER_1K = {
    "gpt-5.6-terra": {"prompt": 0.00125, "completion": 0.010},
    "claude-sonnet-5": {"prompt": 0.0030, "completion": 0.0150},
    "gemini-3.6-flash": {"prompt": 0.0003, "completion": 0.0015},
}


# ----------------------------------------------------------------------
# Pure serialization / parsing helpers (unit-testable without a network).
# ----------------------------------------------------------------------


def to_input_items(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Serialize ChatMessages into Concentrate ``input`` items."""
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            items.append({"role": "system", "content": m.content})
        elif m.role == "user":
            items.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.content:
                items.append({"role": "assistant", "content": m.content})
            for tc in m.tool_calls or []:
                fn = tc.get("function", {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id", "") or "",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    }
                )
        elif m.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": m.content,
                }
            )
    return items


def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Flatten OpenAI-style tool schemas into the Concentrate flat format.

    OpenAI nested: ``{"type": "function", "function": {...}}``.
    Concentrate flat: ``{"type": "function", "name", "description",
    "parameters", "strict"}``.

    ``strict`` is set explicitly to ``False`` because Concentrate's default
    strict mode requires ``additionalProperties: false`` in every schema,
    which our tool schemas do not declare; server-side argument validation
    still enforces required fields and types.
    """
    out: list[dict[str, Any]] = []
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object"}),
                "strict": False,
            }
        )
    return out


def parse_response_output(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Extract (content, tool_calls) from a non-streaming Responses payload."""
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and block.get("text"):
                    content_parts.append(block["text"])
        elif itype == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
    return "".join(content_parts), tool_calls


def parse_usage(usage: dict[str, Any] | None) -> LLMUsage:
    """Map Concentrate usage (input/output/total tokens) to LLMUsage."""
    if not usage:
        return LLMUsage()
    return LLMUsage(
        prompt_tokens=int(usage.get("input_tokens", 0) or 0),
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
    )


def parse_stream_event(
    data: dict[str, Any], event: str | None = None
) -> dict[str, Any]:
    """Normalize one SSE ``data`` payload into a stable keyed dict.

    The Concentrate docs show two shapes for the same events:
      - ``data.type`` + ``data.delta`` (streaming doc)
      - ``data.event`` + ``data.data.arguments`` (tool-calling doc)
    This helper reads both so the streaming loop never depends on which
    documentation the API is currently following.
    """
    etype = data.get("type") or data.get("event") or event or ""
    nested = data.get("data")
    if isinstance(nested, dict):
        data = {**data, **nested}

    return {
        "type": etype,
        "delta": data.get("delta") or "",
        "arguments": data.get("arguments") or "",
        "call_id": data.get("call_id") or "",
        "name": data.get("name") or "",
        "text": data.get("text") or "",
        "error": data.get("error") or data.get("message") or "",
        "response": data.get("response"),
    }


def _normalize_model(model: str) -> str:
    """Strip a provider slug prefix (``openai/gpt-5.6-terra`` → ``gpt-5.6-terra``)."""
    return model.split("/", 1)[-1] if model else model


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


class ConcentrateProvider(LLMProvider):
    """Concentrate-backed provider using the Responses API over httpx."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        routing: dict[str, Any] | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.CONCENTRATE_API_KEY
        if not self.api_key:
            raise ConfigurationError(
                "CONCENTRATE_API_KEY is not set. Configure it in .env to enable "
                "LLM features via Concentrate."
            )
        self.model = model or settings.CONCENTRATE_MODEL
        self.base_url = (base_url or settings.CONCENTRATE_BASE_URL).rstrip("/")
        self.endpoint = f"{self.base_url}{RESPONSES_PATH}"
        self.temperature = settings.CONCENTRATE_TEMPERATURE
        self.max_tokens = settings.CONCENTRATE_MAX_TOKENS
        self.timeout = settings.CONCENTRATE_TIMEOUT
        self.max_retries = settings.CONCENTRATE_MAX_RETRIES
        self.enable_caching = settings.CONCENTRATE_ENABLE_CACHING
        self.routing = routing if routing is not None else self._default_routing()
        self._client = http_client or httpx.AsyncClient(timeout=self.timeout)
        self._owns_client = http_client is None
        self._last_usage: LLMUsage | None = None

    def _default_routing(self) -> dict[str, Any]:
        settings = get_settings()
        routing: dict[str, Any] = {
            "provider": {"sort": settings.CONCENTRATE_ROUTING_SORT or "performance"}
        }
        fallbacks = [
            slug.strip()
            for slug in settings.CONCENTRATE_FALLBACK_MODELS.split(",")
            if slug.strip()
        ]
        # Fallbacks only make sense against a pinned model; "auto" already
        # gives Concentrate full freedom to route across the model pool.
        if fallbacks and self.model != "auto":
            routing["model"] = {"fallbacks": fallbacks}
        return routing

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _build_body(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "input": to_input_items(messages),
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": stream,
        }
        if max_tokens:
            body["max_output_tokens"] = max_tokens
        if tools:
            body["tools"] = convert_tools(tools)
        if self.routing:
            body["routing"] = self.routing
        if self.enable_caching:
            # OpenAI GPT-5.6-family prompt caching; Concentrate converts this
            # for Anthropic/Bedrock routes.
            body["prompt_cache_options"] = {"mode": "implicit", "ttl": "30m"}
        return body

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except (TypeError, ValueError):
                pass
        return min(2**attempt, 15) + random.uniform(0, 0.5)

    def _map_error(self, resp: httpx.Response) -> AgentError:
        status = resp.status_code
        detail = ""
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500] or ""
        if isinstance(body, dict):
            err = body.get("error") or body.get("message")
            if isinstance(err, dict):
                detail = str(err.get("message") or err)
            elif err:
                detail = str(err)
            else:
                detail = json.dumps(body)[:500]
        elif body:
            detail = str(body)[:500]

        if status == 401:
            return AuthenticationError(
                f"Concentrate authentication failed: {detail or f'status {status}'}"
            )
        if status == 402:
            msg = f"top up your account: {detail}" if detail else "top up your account"
            return AgentError(f"Concentrate: insufficient credits (402): {msg}")
        if status == 429:
            return RateLimitError(f"Concentrate rate limit exceeded: {detail}")
        return AgentError(f"Concentrate API error {status}: {detail}")

    async def _request_json(
        self, body: dict[str, Any], stream: bool
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = self._client.build_request(
                "POST",
                self.endpoint,
                headers=self._headers(),
                json=body,
                timeout=self.timeout,
            )
            try:
                resp = await self._client.send(request, stream=stream)
            except httpx.TransportError as e:
                last_error = e
                if attempt < self.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise AgentError(
                    f"Concentrate request failed: {e}"
                ) from e

            if resp.status_code == 200:
                return resp

            if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                await resp.aread()
                await resp.aclose()
                retry_after = resp.headers.get("retry-after")
                await asyncio.sleep(self._backoff(attempt, retry_after))
                continue

            err = self._map_error(resp)
            if stream:
                await resp.aread()
                await resp.aclose()
            raise err
        raise AgentError(  # pragma: no cover
            f"Concentrate request failed: {last_error}"
        )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        messages = trim_messages(messages)
        body = self._build_body(messages, tools, temperature, max_tokens, stream=False)
        resp = await self._request_json(body, stream=False)
        try:
            data = resp.json()
        except Exception as e:
            raise AgentError(f"Concentrate returned invalid JSON: {e}") from e
        finally:
            await resp.aclose()

        status = data.get("status", "")
        if status in ("failed",):
            raise AgentError(
                f"Concentrate request failed: {data.get('error') or 'provider error'}"
            )

        content, tool_calls = parse_response_output(data)
        usage_obj = parse_usage(data.get("usage"))
        self._last_usage = usage_obj
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            model=data.get("model") or self.model,
            usage={
                "prompt_tokens": usage_obj.prompt_tokens,
                "completion_tokens": usage_obj.completion_tokens,
                "total_tokens": usage_obj.total_tokens,
                "cached_tokens": int(
                    (data.get("usage") or {}).get("input_tokens_details", {}).get(
                        "cached_tokens", 0
                    )
                    or 0
                ),
            },
            finish_reason="completed" if status == "completed" else status,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        messages = trim_messages(messages)
        body = self._build_body(messages, tools, temperature, max_tokens, stream=True)
        resp = await self._request_json(body, stream=True)

        tool_buffers: dict[str, dict[str, str]] = {}
        sse_event: str | None = None
        try:
            async for line in resp.aiter_lines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("event:"):
                    sse_event = stripped[len("event:") :].strip()
                    continue
                if not stripped.startswith("data:"):
                    continue
                payload = stripped[len("data:") :].strip()
                if not payload:
                    continue
                try:
                    ev = parse_stream_event(json.loads(payload), event=sse_event)
                    sse_event = None
                except json.JSONDecodeError:
                    continue

                etype = ev["type"]
                if etype == "response.output_text.delta":
                    if ev["delta"]:
                        yield {"type": "token", "content": ev["delta"]}
                elif etype == "response.function_call_arguments.delta":
                    slot = tool_buffers.setdefault(
                        ev["call_id"],
                        {"id": ev["call_id"], "name": "", "arguments": ""},
                    )
                    slot["arguments"] += ev["delta"] or ""
                elif etype == "response.function_call_arguments.done":
                    slot = tool_buffers.setdefault(
                        ev["call_id"],
                        {"id": ev["call_id"], "name": "", "arguments": ""},
                    )
                    if ev["name"]:
                        slot["name"] = ev["name"]
                    if ev["arguments"]:
                        slot["arguments"] = ev["arguments"]
                elif etype in ("response.completed", "response.done", "done"):
                    self._last_usage = parse_usage(
                        (ev.get("response") or {}).get("usage") or ev.get("usage")
                    )
                    break
                elif etype == "response.failed":
                    raise AgentError(
                        f"Concentrate stream failed: {ev['error'] or 'provider error'}"
                    )
                elif etype == "error":
                    raise AgentError(f"Concentrate stream error: {ev['error']}")

            for slot in tool_buffers.values():
                if slot["name"]:
                    yield {
                        "type": "tool_call",
                        "tool_call": {
                            "id": slot["id"],
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["arguments"] or "{}",
                            },
                        },
                    }
        finally:
            await resp.aclose()

        yield {"type": "done"}

    def cost_estimate(self, usage: LLMUsage) -> float:
        prices = COST_PER_1K.get(
            _normalize_model(self.model) or "", DEFAULT_COST_PER_1K
        )
        return (usage.prompt_tokens / 1000) * prices["prompt"] + (
            usage.completion_tokens / 1000
        ) * prices["completion"]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
