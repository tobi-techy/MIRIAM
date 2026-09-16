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
    # Plain defaults (not Field(default=...)): pyright/pylance do not infer
    # defaults from pydantic's Field(..., default=...), which made every
    # AgentConfig(...) call look like it was missing required arguments.
    model: str = "gpt-4"
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: str | None = None
    tools: list[str] = Field(default_factory=list, description="Available tools")
    memory_types: list[str] = Field(
        default_factory=list, description="Memory types to use"
    )
