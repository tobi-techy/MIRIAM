"""LLM provider abstraction for Miriam Financial Agent.

Supports OpenAI (primary) with a provider interface that keeps the agent
loop provider-agnostic. All calls go through a small interface so a model
switch (OpenAI -> Anthropic -> local) is a config change, not a rewrite.

Key production concerns handled here:
  - Token/cost tracking per call (feeds the cost guard)
  - Streaming (token-by-token) for the SSE layer
  - Structured tool calling (function calling)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AgentError, ConfigurationError

logger = logging.getLogger(__name__)

# Max tokens for the conversation context sent to the LLM. The system
# prompt, tool definitions, and newest turns are always preserved; the
# oldest history is trimmed first when the budget is exceeded.
DEFAULT_MAX_CONTEXT_TOKENS = 12000

# Rough heuristic used when tiktoken is unavailable: ~4 chars per token.
CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Count tokens for a text using tiktoken when available, else heuristic."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // CHARS_PER_TOKEN)


def trim_messages(
    messages: list[ChatMessage],
    max_tokens: int | None = None,
) -> list[ChatMessage]:
    """Trim conversation history to fit within the context window.

    The system message and the most recent message are never dropped.
    History is trimmed oldest-first, then the system prompt is truncated
    as a last resort.
    """
    if not messages:
        return messages

    budget = max_tokens or DEFAULT_MAX_CONTEXT_TOKENS
    if sum(count_tokens(m.content or "") for m in messages) <= budget:
        return messages

    # Keep system (index 0) and latest message always.
    system = messages[0]
    latest = messages[-1]
    middle = messages[1:-1]

    trimmed = []
    used = count_tokens(system.content or "") + count_tokens(latest.content or "")
    # Trim oldest history messages first.
    for m in middle:
        m_tokens = count_tokens(m.content or "")
        if used + m_tokens <= budget:
            trimmed.append(m)
            used += m_tokens

    # If even the system prompt alone is too large, truncate it.
    sys_tokens = count_tokens(system.content or "")
    sys_content = system.content
    while sys_tokens > budget and sys_content:
        sys_content = sys_content[: int(len(sys_content) * 0.9)]
        sys_tokens = count_tokens(sys_content)
    if sys_content != system.content:
        system = ChatMessage(role=system.role, content=sys_content)

    result = [system] + trimmed + [latest]
    logger.info(
        "Trimmed context from %d messages to %d",
        len(messages),
        len(result),
    )
    return result


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMProvider(ABC):
    """Interface for LLM-backed generation."""

    model: str = ""

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Complete a chat conversation. May return tool calls."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat completion.

        Yields dicts with keys: ``{"type": "token"|"tool_call"|"done", ...}``
        """
        ...
        yield {}  # pragma: no cover

    @abstractmethod
    def cost_estimate(self, usage: LLMUsage) -> float:
        """Estimate dollar cost of a call for the cost guard."""
        ...


class OpenAIProvider(LLMProvider):
    """OpenAI-backed provider using the official SDK."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        from openai import AsyncOpenAI

        settings = get_settings()
        self.model = model or settings.OPENAI_MODEL
        api_key = api_key or settings.OPENAI_API_KEY
        if not api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not set. Configure it in .env to enable LLM features."
            )
        self.client = AsyncOpenAI(api_key=api_key)
        self._cost_per_1k = {
            "gpt-4": {"prompt": 0.03, "completion": 0.06},
            "gpt-4o": {"prompt": 0.005, "completion": 0.015},
            "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
            "gpt-4.1": {"prompt": 0.002, "completion": 0.008},
            "gpt-4.1-mini": {"prompt": 0.0004, "completion": 0.0016},
        }

    def _serialize(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            item: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            if m.name:
                item["name"] = m.name
            if m.tool_calls:
                item["tool_calls"] = m.tool_calls
            out.append(item)
        return out

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[dict[str, Any]]:
        """Normalize OpenAI tool_calls into our dict shape."""
        if not raw:
            return []
        calls = []
        for tc in raw:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            calls.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": fn.name,
                        "arguments": fn.arguments,
                    },
                }
            )
        return calls

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        settings = get_settings()
        messages = trim_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize(messages),
            "temperature": (
                temperature if temperature is not None else settings.OPENAI_TEMPERATURE
            ),
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools

        try:
            response = await self.client.chat.completions.create(**kwargs)
        except Exception as e:
            logger.error("OpenAI completion failed: %s", e)
            raise AgentError(f"LLM call failed: {e}")

        choice = response.choices[0]
        usage = response.usage
        usage_obj = LLMUsage(
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
        )
        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=self._parse_tool_calls(choice.message.tool_calls),
            model=self.model,
            usage={
                "prompt_tokens": usage_obj.prompt_tokens,
                "completion_tokens": usage_obj.completion_tokens,
                "total_tokens": usage_obj.total_tokens,
            },
            finish_reason=choice.finish_reason or "",
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        settings = get_settings()
        messages = trim_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize(messages),
            "temperature": (
                temperature if temperature is not None else settings.OPENAI_TEMPERATURE
            ),
            "stream": True,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools

        stream = await self.client.chat.completions.create(**kwargs)
        tool_calls_buffer: dict[int, dict[str, Any]] = {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if delta.content:
                yield {"type": "token", "content": delta.content}
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if tc.index is not None else 0
                    slot = tool_calls_buffer.setdefault(
                        idx,
                        {"id": None, "name": "", "arguments": ""},
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    fn = tc.function
                    if fn:
                        if fn.name:
                            slot["name"] += fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
        if tool_calls_buffer:
            for slot in tool_calls_buffer.values():
                if slot["name"]:
                    yield {
                        "type": "tool_call",
                        "tool_call": {
                            "id": slot["id"],
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["arguments"],
                            },
                        },
                    }
        yield {"type": "done"}

    def cost_estimate(self, usage: LLMUsage) -> float:
        prices = self._cost_per_1k.get(
            self.model, {"prompt": 0.005, "completion": 0.015}
        )
        return (usage.prompt_tokens / 1000) * prices["prompt"] + (
            usage.completion_tokens / 1000
        ) * prices["completion"]


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Get the process-wide LLM provider singleton."""
    global _provider
    if _provider is None:
        _provider = OpenAIProvider()
    return _provider
