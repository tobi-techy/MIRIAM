"""LLM provider abstraction for Miriam Financial Agent.

Supports OpenAI (primary) with a provider interface that keeps the agent
loop provider-agnostic. All calls go through a small interface so a model
switch (OpenAI -> Anthropic -> local) is a config change, not a rewrite.

Key production concerns handled here:
  - Token/cost tracking per call (feeds the cost guard)
  - Streaming (token-by-token) for the SSE layer
  - Structured tool calling (function calling)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AgentError, ConfigurationError

logger = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    name: Optional[str] = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
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
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Complete a chat conversation. May return tool calls."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
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

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
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

    def _serialize(self, messages: List[ChatMessage]) -> List[Dict[str, Any]]:
        out = []
        for m in messages:
            item: Dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_call_id:
                item["tool_call_id"] = m.tool_call_id
            if m.name:
                item["name"] = m.name
            if m.tool_calls:
                item["tool_calls"] = m.tool_calls
            out.append(item)
        return out

    @staticmethod
    def _parse_tool_calls(raw: Any) -> List[Dict[str, Any]]:
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
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        settings = get_settings()
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize(messages),
            "temperature": temperature if temperature is not None else settings.OPENAI_TEMPERATURE,
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
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        settings = get_settings()
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self._serialize(messages),
            "temperature": temperature if temperature is not None else settings.OPENAI_TEMPERATURE,
            "stream": True,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools

        stream = await self.client.chat.completions.create(**kwargs)
        tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
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
        prices = self._cost_per_1k.get(self.model, {"prompt": 0.005, "completion": 0.015})
        return (usage.prompt_tokens / 1000) * prices["prompt"] + (
            usage.completion_tokens / 1000
        ) * prices["completion"]


_provider: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Get the process-wide LLM provider singleton."""
    global _provider
    if _provider is None:
        _provider = OpenAIProvider()
    return _provider