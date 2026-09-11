"""Agent loop for Miriam Financial Agent.

The core orchestration engine:

    1. Build system prompt (personality + user context + memory)
    2. Send conversation + tools to LLM
    3. LLM decides: answer directly, or propose tool calls
    4. Tool calls split:
       - read-only / auto-execute: run immediately
       - money movement: DO NOT run; return a confirmation request
    5. Feed tool results back to LLM for the final answer

Safety invariant: no money moves without an explicit user confirmation
that is attached to the exact proposed action.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

from miriam_agent.agents.base import AgentConfig, AgentState, ToolResult
from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.agents.tools import RiskLevel, Tool, ToolRegistry
from miriam_agent.core.exceptions import AgentError, ValidationError
from miriam_agent.integrations.go_client import GoBackendClient
from miriam_agent.safety.policy import SafetyPolicy

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5


@dataclass
class ProposedAction:
    """A money movement the LLM wants to perform, awaiting confirmation."""

    tool_name: str
    arguments: Dict[str, Any]
    display_summary: str


@dataclass
class AgentRunResult:
    response: str
    conversation_id: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    proposed_actions: List[ProposedAction] = field(default_factory=list)
    requires_confirmation: bool = False
    cards: List[Dict[str, Any]] = field(default_factory=list)


class Agent:
    """Stateless agent loop. One instance per request (cheap to build)."""

    def __init__(
        self,
        registry: ToolRegistry,
        provider: Optional[LLMProvider] = None,
        safety_policy: Optional[SafetyPolicy] = None,
        go_client: Optional[GoBackendClient] = None,
        config: Optional[AgentConfig] = None,
    ):
        self.registry = registry
        self.provider = provider or get_llm_provider()
        self.safety_policy = safety_policy or SafetyPolicy()
        self.go_client = go_client
        self.config = config or AgentConfig(name="financial_agent")

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        user_context: Optional[Dict[str, Any]] = None,
        memory_facts: Optional[List[Dict[str, Any]]] = None,
        approved_actions: Optional[List[Dict[str, Any]]] = None,
    ) -> AgentRunResult:
        """Run one user turn. Returns the response plus any proposed actions."""
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
        )

        tool_results: List[Dict[str, Any]] = []
        tool_calls_made: List[Dict[str, Any]] = []
        proposed: List[ProposedAction] = []
        confirmed_executions: List[Dict[str, Any]] = []
        approved_lookup = {
            f"{a.get('tool')}:{json.dumps(a.get('arguments', {}), sort_keys=True)}": True
            for a in approved_actions or []
        }

        # If the user previously approved actions in this request, execute them.
        for a in approved_actions or []:
            tool_name = a.get("tool")
            args = a.get("arguments", {})
            try:
                result = await self._safe_execute(
                    tool_name, args, ctx, user_id, user_context
                )
                confirmed_executions.append(
                    {"tool": tool_name, "arguments": args, "result": result}
                )
                tool_calls_made.append({"name": tool_name, "arguments": args})
            except Exception as e:
                confirmed_executions.append(
                    {"tool": tool_name, "arguments": args, "error": str(e)}
                )

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages)
            for tr in tool_results:
                llm_messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=tr.get("id") or tr.get("tool_name", "tool"),
                        name=tr.get("tool_name", "tool"),
                        content=json.dumps(tr.get("result", {}), default=str)[:2000],
                    )
                )

            schemas = self.registry.llm_schemas()
            response = await self.provider.complete(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            # No tools wanted -> final answer.
            if not response.tool_calls:
                return AgentRunResult(
                    response=response.content,
                    conversation_id=conv_id,
                    tool_calls=tool_calls_made,
                    proposed_actions=proposed,
                    requires_confirmation=bool(proposed),
                )

            # Split tool calls into auto vs. staged.
            stage_next = False
            for call in response.tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}

                tool = self.registry.get(name)
                if tool is None:
                    tool_results.append(
                        {"tool_name": name, "result": {"error": f"unknown tool {name}"}}
                    )
                    tool_calls_made.append({"name": name, "arguments": args})
                    continue

                # Money movement -> stage for confirmation, never auto-run.
                if tool.is_mutation or tool.requires_approval:
                    signature = (
                        f"{name}:{json.dumps(args, sort_keys=True)}"
                    )
                    if signature in approved_lookup or self._matches_pending(args):
                        result = await self._safe_execute(
                            name, args, ctx, user_id, user_context
                        )
                        tool_results.append(
                            {"id": call.get("id"), "tool_name": name, "result": result}
                        )
                        tool_calls_made.append({"name": name, "arguments": args})
                    else:
                        proposed.append(
                            ProposedAction(
                                tool_name=name,
                                arguments=args,
                                display_summary=self._summarize_action(tool, args),
                            )
                        )
                        stage_next = True
                    continue

                # Auto-execute read-only tools.
                tool_calls_made.append({"name": name, "arguments": args})
                try:
                    result = await self._safe_execute(
                        name, args, ctx, user_id, user_context
                    )
                    tool_results.append(
                        {"id": call.get("id"), "tool_name": name, "result": result}
                    )
                except Exception as e:
                    tool_results.append(
                        {"id": call.get("id"), "tool_name": name, "result": {"error": str(e)}}
                    )

            # If a money action is staged, stop the loop and ask user to confirm.
            if stage_next:
                return self._confirmation_result(
                    response=response.content,
                    conv_id=conv_id,
                    proposed=proposed,
                    tool_calls=tool_calls_made,
                    confirmed=confirmed_executions,
                )

        # Tool loop exhausted without a final answer.
        if proposed:
            return self._confirmation_result(
                response="I've prepared the actions below for your approval.",
                conv_id=conv_id,
                proposed=proposed,
                tool_calls=tool_calls_made,
                confirmed=confirmed_executions,
            )
        return AgentRunResult(
            response="I've gathered what you need. Ask me to go further and I will.",
            conversation_id=conv_id,
            tool_calls=tool_calls_made,
        )

    async def stream_run(
        self,
        *,
        user_id: str,
        token: str,
        message: str,
        conversation_id: Optional[str] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        user_context: Optional[Dict[str, Any]] = None,
        memory_facts: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Streaming variant. Yields events: token / tool_result / done / error."""
        messages = self._build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
        )
        ctx = {"user_id": user_id, "token": token}
        schemas = self.registry.llm_schemas()
        tool_results: List[Dict[str, Any]] = []

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages)
            for tr in tool_results:
                llm_messages.append(
                    ChatMessage(
                        role="tool",
                        tool_call_id=tr.get("id") or tr.get("tool_name", "tool"),
                        name=tr.get("tool_name", "tool"),
                        content=json.dumps(tr.get("result", {}), default=str)[:2000],
                    )
                )

            collected: List[str] = []
            stream_tool_calls: List[Dict[str, Any]] = []
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

            stage_next = False
            for call in stream_tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                tool = self.registry.get(name)
                if tool is None:
                    # pass through raw text so model isn't stuck
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": {"error": f"unknown tool {name}"},
                    }
                    continue
                if tool.is_mutation or tool.requires_approval:
                    proposed = self._summarize_action(tool, args)
                    yield {
                        "type": "action_required",
                        "tool": name,
                        "arguments": args,
                        "summary": proposed,
                    }
                    stage_next = True
                    continue
                try:
                    result = await self._safe_execute(name, args, ctx, user_id, None)
                    tool_results.append(
                        {"id": call.get("id"), "tool_name": name, "result": result}
                    )
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "success",
                        "result": result,
                    }
                except Exception as e:
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": {"error": str(e)},
                    }

            if stage_next:
                # Pause streaming so the user can confirm.
                yield {
                    "type": "confirmation_required",
                    "message": "I need your go-ahead before moving money. Review the proposed actions.",
                }
                return

        yield {"type": "error", "message": "Too many tool rounds; stopping safely."}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        message: str,
        history: Optional[List[Dict[str, Any]]],
        user_context: Optional[Dict[str, Any]],
        memory_facts: Optional[List[Dict[str, Any]]],
    ) -> List[ChatMessage]:
        system_prompt = build_system_prompt(
            user_context=user_context,
            memory_facts=memory_facts,
        )
        messages: List[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
        for h in (history or []):
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append(ChatMessage(role=role, content=content))
        messages.append(ChatMessage(role="user", content=message))
        return messages

    async def _safe_execute(
        self,
        name: str,
        args: Dict[str, Any],
        ctx: Dict[str, Any],
        user_id: str,
        user_context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Validate safety policy, then execute via the registry."""
        await self.safety_policy.validate_action(
            tool_name=name,
            arguments=args,
            user_id=user_id,
        )
        result = await self.registry.execute(name, args, context=ctx)
        return result

    def _matches_pending(self, args: Dict[str, Any]) -> bool:
        """Placeholder for sticky pending-action matching in future versions."""
        return False

    def _summarize_action(self, tool: Tool, args: Dict[str, Any]) -> str:
        """Human-readable summary of a proposed money action."""
        if tool.name == "send_money":
            return (
                f"Send {args.get('amount')} to {args.get('to')}"
                + (f" ({args.get('message')})" if args.get("message") else "")
            )
        if tool.name in ("transfer_stash_to_spending", "transfer_spending_to_stash"):
            direction = "stash → spending" if "stash_to_spending" in tool.name else "spending → stash"
            return f"Move {args.get('amount')} ({direction})"
        if tool.name == "execute_investment":
            return f"{args.get('side', 'buy').upper()} {args.get('amount')} of {args.get('symbol')}"
        return f"{tool.name} with {json.dumps(args)}"

    def _confirmation_result(
        self,
        response: str,
        conv_id: str,
        proposed: List[ProposedAction],
        tool_calls: List[Dict[str, Any]],
        confirmed: List[Dict[str, Any]],
    ) -> AgentRunResult:
        cards = [
            {
                "type": "action_confirmation",
                "tool": p.tool_name,
                "arguments": p.arguments,
                "summary": p.display_summary,
            }
            for p in proposed
        ]
        return AgentRunResult(
            response=response,
            conversation_id=conv_id,
            tool_calls=tool_calls,
            proposed_actions=proposed,
            requires_confirmation=True,
            cards=cards,
        )