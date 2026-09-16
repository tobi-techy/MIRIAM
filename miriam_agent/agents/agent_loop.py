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
from miriam_agent.observability.correlation import current_trace_id
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.utils.text import bubble_sets, lift_reaction

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5


def _tool_call_record(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One executed tool call, tagged with the request's trace id.

    The record is what a caller (and the audit trail) uses to reconstruct
    "what did this message actually do", so the id travels with it.
    """
    return {"name": name, "arguments": args, "trace_id": current_trace_id()}


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
    # Chatty-turn affordances the Go executor relays as native gestures: up to
    # two short wrapper bubbles behind the main reply, and one tapback reaction
    # on the user's message. Empty when the reply stayed one message.
    messages: list[str] = field(default_factory=list)
    reaction: str = ""
    # Correlation id for the request this result belongs to, defaulted from the
    # bound context so every construction site carries it without plumbing.
    trace_id: str = field(default_factory=current_trace_id)


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

    def _decorate(self, result: AgentRunResult) -> AgentRunResult:
        """Project the chatty-turn affordances onto a finished result: split a
        wall-of-text reply into short bubbles and lift any whitelisted tapback.
        Confirmation requests stay bare -- a money decision must never be buried
        under bubbly wrappers."""
        if result.requires_confirmation:
            return result
        main, extras = bubble_sets(result.response)
        result.response = main
        result.messages = extras
        result.reaction = lift_reaction(result.response)
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

        tool_calls_made: list[dict[str, Any]] = []
        proposed: list[ProposedAction] = []
        confirmed_executions: list[dict[str, Any]] = []
        # Accumulated assistant-tool_calls + tool-result messages sent to the
        # provider so multi-round tool use is a clean call/result pairing
        # (OpenAI and Concentrate both reject tool results without the
        # preceding assistant tool_calls message).
        llm_extra: list[ChatMessage] = []
        self._approved_actions = list(approved_actions or [])
        approved_lookup = {
            self._signature(str(a.get("tool") or ""), a.get("arguments", {})): True
            for a in self._approved_actions
        }

        # If the user previously approved actions in this request, execute them.
        # Results are cached by signature so the tool loop never re-executes
        # an already-run money action (idempotency guard within the turn).
        executed_results: dict[str, dict[str, Any]] = {}
        for a in approved_actions or []:
            tool_name = str(a.get("tool") or "")
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
                tool_calls_made.append(_tool_call_record(tool_name, args))
            except Exception as e:
                confirmed_executions.append(
                    {"tool": tool_name, "arguments": args, "error": str(e)}
                )

        for _round in range(MAX_TOOL_ROUNDS):
            llm_messages = list(messages) + llm_extra

            schemas = self.registry.llm_schemas()
            response = await self.provider.complete(
                messages=llm_messages,
                tools=schemas,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )

            # No tools wanted -> final answer.
            if not response.tool_calls:
                return self._decorate(
                    AgentRunResult(
                        response=response.content,
                        conversation_id=conv_id,
                        tool_calls=tool_calls_made,
                        proposed_actions=proposed,
                        requires_confirmation=bool(proposed),
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
                    llm_extra.append(
                        self._tool_message(
                            call.get("id"), name, {"error": f"unknown tool {name}"}
                        )
                    )
                    tool_calls_made.append(_tool_call_record(name, args))
                    continue

                # Money movement -> stage for confirmation, never auto-run.
                if tool.is_mutation or tool.requires_approval:
                    signature = self._signature(name, args)
                    if signature in approved_lookup and signature in executed_results:
                        # Already executed for an explicit approval; reuse the
                        # result so the LLM can narrate it without running the
                        # money action a second time.
                        result = executed_results[signature]
                        llm_extra.append(
                            self._tool_message(call.get("id"), name, result)
                        )
                    elif signature in approved_lookup or self._matches_pending(
                        name, args
                    ):
                        try:
                            result = await self._safe_execute(
                                name, args, ctx, user_id, user_context
                            )
                            executed_results[signature] = result
                            llm_extra.append(
                                self._tool_message(call.get("id"), name, result)
                            )
                        except Exception as exc:
                            # An approved money action failed to execute (e.g. the
                            # Go backend was unreachable). Never 500: record the
                            # error as a tool result so the LLM can tell the user
                            # plainly, and do NOT mark it executed so a retry can
                            # attempt it again.
                            llm_extra.append(
                                self._tool_message(
                                    call.get("id"),
                                    name,
                                    {"error": f"{name} failed to execute: {exc}"},
                                )
                            )
                        tool_calls_made.append(_tool_call_record(name, args))
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
            return self._decorate(
                self._confirmation_result(
                    response="I've prepared the actions below for your approval.",
                    conv_id=conv_id,
                    proposed=proposed,
                    tool_calls=tool_calls_made,
                    confirmed=confirmed_executions,
                )
            )
        return self._decorate(
            AgentRunResult(
                response=(
                    "I've gathered what you need. Ask me to go further " "and I will."
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
        approved_actions: list[dict[str, Any]] | None = None,
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
        llm_extra: list[ChatMessage] = []

        # OTP-confirmed action replay: execute approved actions eagerly and
        # cache by signature (as in run()), so the stream never re-runs an
        # already-executed money action and never waits for the LLM to re-emit
        # the exact call. Narrate from the cached results instead.
        self._approved_actions = list(approved_actions or [])
        approved_lookup = {
            self._signature(str(a.get("tool") or ""), a.get("arguments", {})): True
            for a in self._approved_actions
        }
        executed_results: dict[str, dict[str, Any]] = {}
        for a in approved_actions or []:
            tool_name = str(a.get("tool") or "")
            args = a.get("arguments", {})
            sig = self._signature(tool_name, args)
            try:
                result = await self._safe_execute(
                    tool_name, args, ctx, user_id, None
                )
                executed_results[sig] = result
                yield {
                    "type": "tool_result",
                    "tool": a.get("tool"),
                    "status": "success",
                    "result": result,
                }
            except Exception as e:  # noqa: BLE001
                executed_results[sig] = {"error": str(e)}
                yield {
                    "type": "tool_result",
                    "tool": a.get("tool"),
                    "status": "error",
                    "result": {"error": str(e)},
                }

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
                    llm_extra.append(
                        self._tool_message(
                            call.get("id"), name, {"error": f"unknown tool {name}"}
                        )
                    )
                    yield {
                        "type": "tool_result",
                        "tool": name,
                        "status": "error",
                        "result": {"error": f"unknown tool {name}"},
                    }
                    continue
                if tool.is_mutation or tool.requires_approval:
                    signature = self._signature(name, args)
                    if signature in approved_lookup and signature in executed_results:
                        llm_extra.append(
                            self._tool_message(
                                call.get("id"),
                                name,
                                executed_results[signature],
                            )
                        )
                        yield {
                            "type": "tool_result",
                            "tool": name,
                            "status": "success",
                            "result": executed_results[signature],
                        }
                    elif signature in approved_lookup or self._matches_pending(
                        name, args
                    ):
                        try:
                            result = await self._safe_execute(
                                name, args, ctx, user_id, None
                            )
                            executed_results[signature] = result
                            llm_extra.append(
                                self._tool_message(call.get("id"), name, result)
                            )
                            yield {
                                "type": "tool_result",
                                "tool": name,
                                "status": "success",
                                "result": result,
                            }
                        except Exception as e:  # noqa: BLE001
                            llm_extra.append(
                                self._tool_message(
                                    call.get("id"), name, {"error": str(e)}
                                )
                            )
                            yield {
                                "type": "tool_result",
                                "tool": name,
                                "status": "error",
                                "result": {"error": str(e)},
                            }
                    else:
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

            if stage_next:
                # Pause streaming so the user can confirm.
                yield {
                    "type": "confirmation_required",
                    "message": (
                        "I need your go-ahead before moving money. "
                        "Review the proposed actions."
                    ),
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
        exec_ctx = dict(ctx)
        # Deterministic per-(user, tool, args) idempotency key so a retried
        # money action cannot double-execute on the Go side.
        _tool = self.registry.get(name)
        if _tool is not None and (_tool.is_mutation or _tool.requires_approval):
            exec_ctx["idempotency_key"] = self._idempotency_key(
                str(ctx.get("user_id", "")), name, args
            )
        result = await self.registry.execute(name, args, context=exec_ctx)
        return await self._replay_staged_confirmation(name, args, exec_ctx, result)

    async def _replay_staged_confirmation(
        self,
        name: str,
        args: dict[str, Any],
        exec_ctx: dict[str, Any],
        result: Any,
    ) -> Any:
        """Replay a Go-staged action with the confirmation token it just issued.

        Rail's investment API answers a mutation with a preview plus a
        payload-bound confirmation token (HTTP 202) instead of executing it.
        Miriam only reaches execution after the user approved the action, so the
        token is replayed once with the identical payload. Actions whose policy
        verdict needs an in-app passcode are never auto-replayed.
        """
        if not isinstance(result, dict):
            return result
        if result.get("status") != "AWAITING_CONFIRMATION":
            return result

        from miriam_agent.tools.investment_definitions import (
            STAGED_CONFIRMATION_TOOLS,
        )

        if name not in STAGED_CONFIRMATION_TOOLS:
            return result

        token = (result.get("confirmation") or {}).get("token")
        if not token:
            return result

        verdict = str((result.get("policy") or {}).get("verdict", "")).upper()
        if verdict == "REQUIRES_AUTHENTICATION":
            return {
                **result,
                "error": (
                    f"'{name}' needs an in-app passcode confirmation before it can "
                    "run. It was not executed."
                ),
            }

        replay_ctx = dict(exec_ctx)
        replay_ctx["confirmation_token"] = token
        replayed = await self.registry.execute(name, args, context=replay_ctx)
        if replayed.get("status") == "AWAITING_CONFIRMATION":
            return {
                **replayed,
                "error": (
                    f"'{name}' still reported AWAITING_CONFIRMATION after the "
                    "confirmation replay. Nothing further ran."
                ),
            }
        return replayed

    def _idempotency_key(
        self, user_id: str, tool_name: str, args: dict[str, Any]
    ) -> str:
        """Deterministic idempotency key for a mutation (user-scoped)."""
        import hashlib

        normalized_args = json.dumps(self._normalize(args), sort_keys=True)
        digest = hashlib.sha256(
            f"{user_id}:{tool_name}:{normalized_args}".encode()
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
            self._signature(str(a.get("tool") or ""), a.get("arguments", {}))
            for a in (self._approved_actions or [])
        }

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
        if tool.name in ("buy_asset", "sell_asset"):
            side = "BUY" if tool.name == "buy_asset" else "SELL"
            target = args.get("symbol") or args.get("asset_id")
            return f"{side} ${args.get('amount_usd')} of {target}"
        if tool.name == "set_allocation":
            targets = args.get("targets") or []
            return f"Set allocation across {len(targets)} assets"
        if tool.name == "create_strategy":
            return f"Create strategy {args.get('name')} (risk: {args.get('risk')})"
        if tool.name == "update_strategy":
            return f"Update strategy {args.get('strategy_id')}"
        if tool.name == "enroll_strategy":
            amount = args.get("amount_usd")
            suffix = f" with ${amount}" if amount is not None else ""
            return f"Enroll in strategy {args.get('strategy_id')}{suffix}"
        if tool.name in ("pause_strategy", "resume_strategy"):
            verb = "Pause" if tool.name == "pause_strategy" else "Resume"
            return f"{verb} strategy {args.get('strategy_id')}"
        if tool.name == "rebalance_strategy":
            return f"Rebalance strategy {args.get('strategy_id')}"
        if tool.name == "pay_bill":
            return (
                f"Pay {args.get('amount_ngn')} NGN of {args.get('category')} "
                f"for {args.get('recipient')}"
            )
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
