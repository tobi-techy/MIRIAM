"""Agent loop for Miriam Financial Agent.

The core orchestration engine, for answerable turns only:

    1. Build system prompt (personality + user context + memory)
    2. Send conversation + read-only tools to the LLM
    3. LLM decides: answer directly, or propose a read
    4. Reads run. Nothing else can: see the invariant below.
    5. Feed tool results back to the LLM for the final answer

**This loop cannot move money, and that is structural.**

Three independent things would have to fail for it to reach a rail:

* the registry it reads from has no money tool in it
  (``tools/definitions.build_tool_registry`` removes them, and
  ``ToolRegistry.llm_schemas`` only ever offers read-only tools),
* :meth:`Agent._safe_execute` refuses a mutation outright rather than staging
  it, so there is no approval path to talk it into running one,
* the money tools themselves are only reachable through
  ``miriam_agent.hands``, which this module does not import.

Money turns never arrive here at all. ``api/chat.py`` classifies the turn first
and sends anything that touches money to ``orchestrator.py``, whose only path is
Hands -> Judgment -> Hands -> Voice.

The earlier version of this file staged money actions, held an approval ledger
and replayed Go confirmation tokens. All of that is gone: there were two writers
of a balance, and this was the wrong one.
"""

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.base import AgentConfig
from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.agents.tools import ToolRegistry
from miriam_agent.integrations.go_client import GoBackendClient
from miriam_agent.judgment.client import enabled as typesafe_enabled
from miriam_agent.judgment.gates import (
    EGRESS_DONT_KNOW,
    EGRESS_REGENERATE_INSTRUCTION,
    EgressBranch,
    EgressDecision,
    ToolBranch,
    ToolDecision,
    build_state,
    egress_gate,
    tool_gate,
)
from miriam_agent.judgment.schemas import ProposedTool
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.utils.text import bubble_sets, clean_text, lift_reaction

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5

# The refusal a model gets when it proposes a tool it cannot have. It is a tool
# result rather than an error so the model can answer the user normally.
_MONEY_TOOL_REFUSAL = (
    "'{name}' moves money and is not available here. Money movements go "
    "through the ledger, not through a tool call. Nothing ran."
)


def _tool_call_record(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One executed tool call, tagged with the request's trace id.

    The record is what a caller (and the audit trail) uses to reconstruct
    "what did this message actually do", so the id travels with it.
    """
    return {"name": name, "arguments": args, "trace_id": current_trace_id()}


@dataclass
class AgentRunResult:
    response: str
    conversation_id: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Chatty-turn affordances the Go executor relays as native gestures: up to
    # two short wrapper bubbles behind the main reply, and one tapback reaction
    # on the user's message. Empty when the reply stayed one message.
    messages: list[str] = field(default_factory=list)
    reaction: str = ""
    # Correlation id for the request this result belongs to, defaulted from the
    # bound context so every construction site carries it without plumbing.
    trace_id: str = field(default_factory=current_trace_id)


class Agent:
    """Stateless agent loop for answerable turns. One instance per request."""

    def __init__(
        self,
        registry: ToolRegistry,
        provider: LLMProvider | None = None,
        safety_policy: SafetyPolicy | None = None,
        go_client: GoBackendClient | None = None,
        config: AgentConfig | None = None,
    ):
        self.registry = registry
        self.provider = provider or get_llm_provider()
        self.safety_policy = safety_policy or SafetyPolicy()
        self.go_client = go_client
        self.config = config or AgentConfig(name="financial_agent")

    def _decorate(self, result: AgentRunResult) -> AgentRunResult:
        """Project the chatty-turn affordances onto a finished result: split a
        wall-of-text reply into short bubbles and lift any whitelisted tapback.
        Every user-facing response is scrubbed first so no em/en dash the model
        emitted ever reaches the user."""
        clean = clean_text(result.response or "")
        main, extras = bubble_sets(clean)
        result.response = main
        result.messages = [clean_text(m) for m in extras]
        result.reaction = lift_reaction(clean)
        return result

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_context: dict[str, Any] | None = None,
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> AgentRunResult:
        """Run one answerable turn. Returns the reply and the reads it made."""
        conv_id = conversation_id or f"conv_{user_id}"
        ctx = {
            "user_id": user_id,
            "token": token,
        }
        messages = self._build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )

        tool_calls_made: list[dict[str, Any]] = []
        # Accumulated assistant-tool_calls + tool-result messages sent to the
        # provider so multi-round tool use is a clean call/result pairing
        # (OpenAI and Concentrate both reject tool results without the
        # preceding assistant tool_calls message).
        llm_extra: list[ChatMessage] = []

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages) + llm_extra

            schemas = self.registry.llm_schemas()
            response = await self.provider.complete(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            # No tools wanted -> final answer, gated before send.
            if not response.tool_calls:
                reply = await self._apply_egress(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    draft=response.content,
                    llm_messages=llm_messages,
                    tool_results=self._collect_tool_results(llm_extra),
                )
                return self._decorate(
                    AgentRunResult(
                        response=reply,
                        conversation_id=conv_id,
                        tool_calls=tool_calls_made,
                    )
                )

            # Record the assistant's tool_calls message so the next round is a
            # clean call -> result pairing (OpenAI and Concentrate both reject
            # tool results without the preceding assistant tool_calls message).
            llm_extra.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            for call in response.tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    args = {}

                tool = self.registry.get(name)
                if tool is None:
                    llm_extra.append(
                        self._tool_message(
                            call.get("id"), name, self._unknown_tool_result(name)
                        )
                    )
                    tool_calls_made.append(_tool_call_record(name, args))
                    continue

                # TypeSafe tool gate: reject irrelevant/bad-arg calls before
                # anything runs. A tool the gate wants confirmed or blocked is
                # not run either: with no money path left in this loop, "confirm"
                # can only mean "do not do this silently", and the answer is no.
                gate = await self._tool_gate_decision(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    name=name,
                    args=args,
                )
                if gate.branch is not ToolBranch.ALLOW:
                    llm_extra.append(
                        self._tool_message(
                            call.get("id"),
                            name,
                            {
                                "error": (
                                    f"tool call not run ({gate.branch.value}): "
                                    f"{gate.reason or 'not permitted here'}"
                                )
                            },
                        )
                    )
                    tool_calls_made.append(_tool_call_record(name, args))
                    continue

                tool_calls_made.append(_tool_call_record(name, args))
                try:
                    result = await self._safe_execute(
                        name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                except Exception as e:
                    llm_extra.append(
                        self._tool_message(call.get("id"), name, {"error": str(e)})
                    )

        # Tool loop exhausted without a final answer.
        return self._decorate(
            AgentRunResult(
                response=(
                    "I've gathered what you need. Ask me to go further and I will."
                ),
                conversation_id=conv_id,
                tool_calls=tool_calls_made,
            )
        )

    async def stream_run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_context: dict[str, Any] | None = None,
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Streaming variant. Yields events: token / tool_result / done / error.

        Money turns do not reach here; ``api/chat.py`` routes them to the
        orchestrator. A model that still asks for a money tool is refused, and
        the refusal is streamed as a tool result so it can answer the user.
        """
        messages = self._build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )
        ctx = {"user_id": user_id, "token": token}
        schemas = self.registry.llm_schemas()
        llm_extra: list[ChatMessage] = []

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages) + llm_extra

            collected: list[str] = []
            stream_tool_calls: list[dict[str, Any]] = []
            async for event in self.provider.stream(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            ):
                if event["type"] == "token":
                    collected.append(event["content"])
                    yield {"type": "token", "content": event["content"]}
                elif event["type"] == "tool_call":
                    tc = event["tool_call"]
                    yield {"type": "tool_call", "tool_call": tc}
                    stream_tool_calls.append(tc)

            if not stream_tool_calls:
                yield {"type": "done", "content": "".join(collected)}
                return

            # Record the assistant's tool_calls so the next round is a clean
            # call -> result pairing (required by OpenAI and Concentrate).
            llm_extra.append(
                ChatMessage(
                    role="assistant",
                    content="".join(collected),
                    tool_calls=stream_tool_calls,
                )
            )

            for call in stream_tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
                except json.JSONDecodeError:
                    args = {}
                tool = self.registry.get(name)
                if tool is None:
                    result = self._unknown_tool_result(name)
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": result,
                    }
                    continue

                gate = await self._tool_gate_decision(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    name=name,
                    args=args,
                )
                if gate.branch is not ToolBranch.ALLOW:
                    result = {
                        "error": (
                            f"tool call not run ({gate.branch.value}): "
                            f"{gate.reason or 'not permitted here'}"
                        )
                    }
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": result,
                    }
                    continue

                try:
                    result = await self._safe_execute(
                        name, args, ctx, user_id, user_context
                    )
                    llm_extra.append(self._tool_message(call.get("id"), name, result))
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "success",
                        "result": result,
                    }
                except Exception as e:
                    llm_extra.append(
                        self._tool_message(call.get("id"), name, {"error": str(e)})
                    )
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": {"error": str(e)},
                    }

        yield {"type": "error", "message": "Too many tool rounds; stopping safely."}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        message: str,
        history: list[dict[str, Any]] | None,
        user_context: dict[str, Any] | None,
        memory_facts: list[dict[str, Any]] | None,
        financial_plan: dict[str, Any] | None = None,
    ) -> list[ChatMessage]:
        system_prompt = build_system_prompt(
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt)
        ]
        for h in history or []:
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append(ChatMessage(role=role, content=content))
        messages.append(ChatMessage(role="user", content=message))
        return messages

    @staticmethod
    def _unknown_tool_result(name: str) -> dict[str, Any]:
        """What a model gets when it asks for a tool that is not available.

        A money tool names itself, so the refusal can say what it was and why,
        rather than the generic "unknown tool" a model might try to work around.
        """
        if name in MONEY_TOOL_NAMES:
            return {"error": _MONEY_TOOL_REFUSAL.format(name=name), "_blocked": True}
        return {"error": f"unknown tool {name}"}

    async def _tool_gate_decision(
        self,
        *,
        user_id: str,
        message: str,
        history: list[dict[str, Any]] | None,
        user_context: dict[str, Any] | None,
        name: str,
        args: dict[str, Any],
    ) -> ToolDecision:
        """Judge one proposed tool call before anything runs."""
        if not typesafe_enabled():
            return ToolDecision(branch=ToolBranch.ALLOW, degraded=True)
        return await tool_gate(
            build_state(
                user_id=user_id,
                message=message,
                history=history,
                user_context=user_context,
                registry=self.registry,
                proposed_tool=ProposedTool(name=name, args=args),
            )
        )

    async def _apply_egress(
        self,
        *,
        user_id: str,
        message: str,
        history: list[dict[str, Any]] | None,
        user_context: dict[str, Any] | None,
        draft: str,
        llm_messages: list[ChatMessage],
        tool_results: list[dict[str, Any]],
    ) -> str:
        """Run the egress gate on a draft and return the reply to send."""
        if not typesafe_enabled():
            return draft

        async def _evaluate(text: str) -> EgressDecision:
            return await egress_gate(
                build_state(
                    user_id=user_id,
                    message=message,
                    history=history,
                    user_context=user_context,
                    registry=self.registry,
                    draft_reply=text,
                    tool_results=tool_results,
                )
            )

        decision = await _evaluate(draft)
        if decision.branch is EgressBranch.DISCARD:
            return decision.reply or EGRESS_DONT_KNOW
        if decision.branch is EgressBranch.REGENERATE:
            try:
                corrected = await self.provider.complete(
                    messages=llm_messages
                    + [ChatMessage(role="user", content=EGRESS_REGENERATE_INSTRUCTION)],
                    tools=None,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                regenerated = corrected.content or ""
            except Exception:
                logger.warning("egress regeneration failed", exc_info=True)
                return EGRESS_DONT_KNOW
            second = await _evaluate(regenerated)
            if second.branch is EgressBranch.SEND:
                return regenerated
            return EGRESS_DONT_KNOW
        return draft

    @staticmethod
    def _collect_tool_results(llm_extra: list[ChatMessage]) -> list[dict[str, Any]]:
        """Tool results from this turn, for the egress grounding check."""
        return [
            {"name": m.name or "tool", "result": m.content}
            for m in llm_extra
            if m.role == "tool"
        ]

    async def _safe_execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: dict[str, Any],
        user_id: str,
        user_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Enforce RBAC and safety policy, then execute. Reads only.

        The mutation refusal is the load-bearing line in this file. It is not a
        policy check that some future flag could relax: a tool that changes money
        state is returned as blocked, whatever the caller says, because this loop
        has no way to authorise one.
        """
        from miriam_agent.auth.rbac import require_tool_access

        tool = self.registry.get(name)
        if tool is None:
            return self._unknown_tool_result(name)
        if tool.is_mutation or tool.requires_approval or name in MONEY_TOOL_NAMES:
            logger.error(
                "refusing a money tool on the answer path",
                extra={"tool": name, "user_id": user_id},
            )
            return {
                "error": _MONEY_TOOL_REFUSAL.format(name=name),
                "_blocked": True,
            }

        roles = set((user_context or {}).get("roles") or ["guest"])
        require_tool_access(roles, name)
        allowed = await self.safety_policy.validate_action(
            tool_name=name,
            arguments=args,
            user_id=user_id,
            financial_profile=user_context,
        )
        if not allowed:
            logger.info(
                "Tool denied by safety policy",
                extra={"tool": name, "user_id": user_id},
            )
            return {
                "error": f"'{name}' was blocked by safety checks. Nothing ran.",
                "_blocked": True,
            }

        return await self.registry.execute(name, args, context=ctx)

    @staticmethod
    def _tool_message(
        call_id: str | None, tool_name: str, result: dict[str, Any]
    ) -> ChatMessage:
        """Build a tool-result ChatMessage (content truncated for context budget)."""
        return ChatMessage(
            role="tool",
            tool_call_id=call_id or tool_name or "tool",
            name=tool_name or "tool",
            content=json.dumps(result, default=str)[:2000],
        )
