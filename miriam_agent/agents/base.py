"""Shared agent configuration.

This module used to also hold ``BaseAgent`` (an abstract agent class from
an early, non-Go-aware execution model) plus ``AgentState``, ``ToolCall``,
and ``ToolResult``. None of that was used by the live system --
``agents/agent_loop.py::Agent`` is the real agent loop -- so it was removed
to avoid confusing future readers about which agent implementation
actually runs. Only ``AgentConfig`` survives: it's the small settings
object the live agent loop and the chat API still construct per request.
"""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Configuration for an agent."""

    name: str = Field(..., description="Agent name")
    model: str = Field("gpt-4", description="Model to use")
    temperature: float = Field(0.7, description="Temperature for generation")
    max_tokens: int = Field(4096, description="Maximum tokens per response")
    system_prompt: str | None = Field(None, description="System prompt")
    tools: list[str] = Field(default_factory=list, description="Available tools")
    memory_types: list[str] = Field(
        default_factory=list, description="Memory types to use"
    )
