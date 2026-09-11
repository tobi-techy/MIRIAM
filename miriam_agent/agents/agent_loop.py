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
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from miriam_agent.agents.base import AgentConfig
from miriam_agent.agents.llm import ChatMessage, LLMProvider, get_llm_provider
from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.agents.tools import Tool, ToolRegistry
from miriam_agent.integrations.go_client import GoBackendClient
from miriam_agent.safety.policy import SafetyPolicy

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5


@dataclass
class ProposedAction:
    """A money movement the LLM wants to perform, awaiting confirmation."""

    tool_name: str
    arguments: dict[str, Any]
    display_summary: str


@dataclass
class AgentRunResult:
    response: str
    conversation_id: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    proposed_actions: list[ProposedAction] = field(default_factory=list)
    requires_confirmation: bool = False
    cards: list[dict[str, Any]] = field(default_factory=list)


class Agent:
    """Stateless agent loop. One instance per request (cheap to build)."""

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
        approved_actions: list[dict[str, Any]] | None = None,
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
            financial_plan=financial_plan,
        )

        tool_results: list[dict[str, Any]] = []
        tool_calls_made: list[dict[str, Any]] = []
        proposed: list[ProposedAction] = []
        confirmed_executions: list[dict[str, Any]] = []
        self._approved_actions = list(approved_actions or [])
        approved_lookup = {
            self._signature(a.get("tool"), a.get("arguments", {})): True
            for a in self._approved_actions
        }

        # If the user previously approved actions in this request, execute them.
        # Results are cached by signature so the tool loop never re-executes
        # an already-run money action (idempotency guard within the turn).
        executed_results: dict[str, dict[str, Any]] = {}
        for a in approved_actions or []:
            tool_name = a.get("tool")
            args = a.get("arguments", {})
            sig = self._signature(tool_name, args)
            try:
                result = await self._safe_execute(
                    tool_name, args, ctx, user_id, user_context
                )
                executed_results[sig] = result
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
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    )
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
                    signature = self._signature(name, args)
                    if signature in approved_lookup and signature in executed_results:
                        # Already executed for an explicit approval; reuse the
                        # result so the LLM can narrate it without running the
                        # money action a second time.
                        result = executed_results[signature]
                        tool_results.append(
                            {"id": call.get("id"), "tool_name": name, "result": result}
                        )
                    elif signature in approved_lookup or self._matches_pending(
                        name, args
                    ):
                        result = await self._safe_execute(
                            name, args, ctx, user_id, user_context
                        )
                        executed_results[signature] = result
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
                        {
                            "id": call.get("id"),
                            "tool_name": name,
                            "result": {"error": str(e)},
                        }
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
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_context: dict[str, Any] | None = None,
        memory_facts: list[dict[str, Any]] | None = None,
        financial_plan: dict[str, Any] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Streaming variant. Yields events: token / tool_result / done / error."""
        messages = self._build_messages(
            message=message,
            history=history,
            user_context=user_context,
            memory_facts=memory_facts,
            financial_plan=financial_plan,
        )
        ctx = {"user_id": user_id, "token": token}
        schemas = self.registry.llm_schemas()
        tool_results: list[dict[str, Any]] = []

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

            stage_next = False
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

    async def _safe_execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: dict[str, Any],
        user_id: str,
        user_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Enforce RBAC, validate safety policy, then execute via the registry."""
        from miriam_agent.auth.rbac import require_tool_access

        roles = set((user_context or {}).get("roles") or ["guest"])
        require_tool_access(roles, name)
        await self.safety_policy.validate_action(
            tool_name=name,
            arguments=args,
            user_id=user_id,
        )
        exec_ctx = dict(ctx)
        # Deterministic per-(user, tool, args) idempotency key so a retried
        # money action cannot double-execute on the Go side.
        if self.registry.get(name) is not None and (
            self.registry.get(name).is_mutation
            or self.registry.get(name).requires_approval
        ):
            exec_ctx["idempotency_key"] = self._idempotency_key(
                str(ctx.get("user_id", "")), name, args
            )
        result = await self.registry.execute(name, args, context=exec_ctx)
        return result

    def _idempotency_key(
        self, user_id: str, tool_name: str, args: dict[str, Any]
    ) -> str:
        """Deterministic idempotency key for a mutation (user-scoped)."""
        import hashlib

        digest = hashlib.sha256(
            f"{user_id}:{tool_name}:{json.dumps(self._normalize(args), sort_keys=True)}".encode()
        ).hexdigest()[:32]
        return f"miriam:{tool_name}:{digest}"

    @staticmethod
    def _normalize(value: Any) -> Any:
        """Recursively normalize a value so int and float representations of
        the same number (100 vs 100.0) produce identical signatures."""
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: Agent._normalize(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [Agent._normalize(v) for v in value]
        return value

    def _signature(self, tool_name: str, args: dict[str, Any]) -> str:
        """Stable, type-tolerant signature for an action proposal."""
        return f"{tool_name}:{json.dumps(self._normalize(args), sort_keys=True)}"

    def _matches_pending(self, tool_name: str, args: dict[str, Any]) -> bool:
        """Whether the proposed action matches an already-approved action.

        Uses type-tolerant signatures so the LLM re-emitting an approved
        action with e.g. 100.0 instead of 100 does not require a second
        confirmation.
        """
        sig = self._signature(tool_name, args)
        return sig in {
            self._signature(a.get("tool"), a.get("arguments", {}))
            for a in (self._approved_actions or [])
        }

    def _summarize_action(self, tool: Tool, args: dict[str, Any]) -> str:
        """Human-readable summary of a proposed money action."""
        if tool.name == "send_money":
            return f"Send {args.get('amount')} to {args.get('to')}" + (
                f" ({args.get('message')})" if args.get("message") else ""
            )
        if tool.name in ("transfer_stash_to_spending", "transfer_spending_to_stash"):
            direction = (
                "stash → spending"
                if "stash_to_spending" in tool.name
                else "spending → stash"
            )
            return f"Move {args.get('amount')} ({direction})"
        if tool.name == "execute_investment":
            return f"{args.get('side', 'buy').upper()} {args.get('amount')} of {args.get('symbol')}"
        return f"{tool.name} with {json.dumps(args)}"

    def _confirmation_result(
        self,
        response: str,
        conv_id: str,
        proposed: list[ProposedAction],
        tool_calls: list[dict[str, Any]],
        confirmed: list[dict[str, Any]],
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
