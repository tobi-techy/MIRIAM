import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from miriam_agent.core.exceptions import AgentError
from miriam_agent.database.models import FinancialProfile, MemoryEntry
from miriam_agent.safety.policy import SafetyPolicy

logger = logging.getLogger(__name__)


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


class AgentState(BaseModel):
    """State of an agent."""

    conversation_id: str
    user_id: str
    current_topic: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    last_action: str | None = None
    session_start: float = Field(
        default_factory=lambda: asyncio.get_event_loop().time()
    )


class ToolCall(BaseModel):
    """Represents a tool call by the agent."""

    name: str = Field(..., description="Tool name")
    arguments: dict[str, Any] = Field(..., description="Tool arguments")
    id: str | None = Field(None, description="Tool call ID")
    type: str = Field("function", description="Tool type")


class ToolResult(BaseModel):
    """Represents a tool result."""

    content: str = Field(..., description="Result content")
    tool_name: str = Field(..., description="Tool name")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )


class BaseAgent(ABC):
    """Base class for all agents."""

    def __init__(
        self,
        config: AgentConfig,
        safety_policy: SafetyPolicy,
        memory_store: Any,
        financial_profile: FinancialProfile,
    ):
        self.config = config
        self.safety_policy = safety_policy
        self.memory_store = memory_store
        self.financial_profile = financial_profile
        self.state = AgentState(
            conversation_id=f"conv_{asyncio.get_event_loop().time()}",
            user_id=financial_profile.user_id,
        )
        self.logger = logging.getLogger(f"{__name__}.{config.name}")

    async def process_message(
        self,
        message: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a user message."""
        try:
            if conversation_id:
                self.state.conversation_id = conversation_id

            self.logger.info(
                "Processing message",
                message=message,
                conversation_id=self.state.conversation_id,
                user_id=self.state.user_id,
            )

            # Store user message in memory
            await self.memory_store.store_interaction(
                user_id=self.state.user_id,
                role="user",
                content=message,
                conversation_id=self.state.conversation_id,
                metadata={"topic": "general"},
            )

            # Plan response using reasoning
            response_plan = await self._plan_response(message)

            # Execute actions based on plan
            tool_results = await self._execute_action_plan(response_plan)

            # Generate final response
            final_response = await self._generate_response(
                message, response_plan, tool_results
            )

            # Store assistant response in memory
            await self.memory_store.store_interaction(
                user_id=self.state.user_id,
                role="assistant",
                content=final_response,
                conversation_id=self.state.conversation_id,
                metadata={
                    "topic": "general",
                    "tool_calls": response_plan.get("tool_calls", []),
                    "tool_results": [result.dict() for result in tool_results],
                },
            )

            return {
                "response": final_response,
                "conversation_id": self.state.conversation_id,
                "metadata": {
                    "processed_at": asyncio.get_event_loop().time(),
                    "tool_calls": response_plan.get("tool_calls", []),
                    "memory_stored": True,
                },
            }

        except Exception as e:
            self.logger.error(
                "Error processing message",
                error=str(e),
                exc_info=True,
            )
            raise AgentError(f"Failed to process message: {str(e)}")

    @abstractmethod
    async def _plan_response(self, message: str) -> dict[str, Any]:
        """Plan how to respond to a user message."""
        pass

    @abstractmethod
    async def _execute_action_plan(
        self, action_plan: dict[str, Any]
    ) -> list[ToolResult]:
        """Execute the planned actions."""
        pass

    @abstractmethod
    async def _generate_response(
        self,
        message: str,
        action_plan: dict[str, Any],
        tool_results: list[ToolResult],
    ) -> str:
        """Generate the final response."""
        pass

    async def _validate_safety(self, action: dict[str, Any]) -> bool:
        """Validate if an action is safe to execute."""
        try:
            # Extract action details
            tool_name = action.get("name", "")
            arguments = action.get("arguments", {})

            # Check against safety policies
            return await self.safety_policy.validate_action(
                tool_name=tool_name,
                arguments=arguments,
                user_id=self.state.user_id,
                financial_profile=self.financial_profile,
            )

        except Exception as e:
            self.logger.error(
                "Error validating safety",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _store_memory(
        self,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ):
        """Store interaction in memory."""
        try:
            await self.memory_store.store_interaction(
                user_id=self.state.user_id,
                role=role,
                content=content,
                conversation_id=self.state.conversation_id,
                metadata=metadata or {},
            )
        except Exception as e:
            self.logger.error(
                "Error storing memory",
                role=role,
                error=str(e),
                exc_info=True,
            )

    async def get_conversation_history(self, limit: int = 10) -> list[MemoryEntry]:
        """Get conversation history."""
        return await self.memory_store.get_recent_interactions(
            user_id=self.state.user_id,
            limit=limit,
        )

    async def update_conversation_id(self, conversation_id: str):
        """Update the conversation ID."""
        self.state.conversation_id = conversation_id
