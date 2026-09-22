"""Layer bridge. ``orchestrator.py`` is the only way in.

Nothing else calls Hands, Judgment and Voice in sequence. A caller creates an
:class:`Orchestrator` and hands it an :class:`Event`; it does not reach into the
layers itself. That single door is what makes the ordering invariant auditable.

The legal path, and the only one implemented here::

    user / event
      -> Hands reads the ledger and the policy (source of truth)
      -> Judgment returns a typed decision
      -> Hands executes, and only if the decision, the policy and the limits allow
      -> Voice narrates STATE after the fact

There is deliberately no path from Voice back into Hands, and no path from Voice
into Judgment. Voice is called last and its output is returned to the caller,
not fed anywhere.

Two rules are enforced structurally rather than documented:

* a proposed action is only ever built from the *user's* words. Prose emitted by
  a model is never parsed for an action, so a model cannot move money by writing
  about moving money.
* a confirmation is a ``confirm_id`` that Hands issued. A chat word is not a
  confirmation, and a confirm id that Hands never created is refused.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from miriam_agent.hands.audit import AuditLog, AuditRow, Receipt
from miriam_agent.hands.invest import (
    INVEST_SLEEVE,
    parse_invest_utterance,
    prepare_allocate,
    settle_allocate,
)
from miriam_agent.hands.ledger import (
    Challenge,
    InMemoryLedgerStore,
    Ledger,
    LedgerStore,
    LedgerUnavailable,
    PendingInflow,
    money,
    new_ledger,
)
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.split import audit_row_for_split, split_inflow
from miriam_agent.hands.state import (
    Execution,
    HandlerState,
    ProposedAction,
    build_state,
)
from miriam_agent.hands.transfer import (
    InMemoryRail,
    Rail,
    TransferOutcome,
    execute_transfer,
    move_between_sleeves,
    parse_transfer_utterance,
    record_refusal,
    route_yield,
)
from miriam_agent.judgment.decide import Judge, decide
from miriam_agent.judgment.schema import Decision
from miriam_agent.voice.generate import UNAVAILABLE_LINE, deterministic_message, speak
from miriam_agent.voice.memory import read_facts

logger = logging.getLogger(__name__)

# How long a confirm_id stays valid.
CHALLENGE_TTL_MINUTES = 30

EventType = Literal["inflow", "utterance", "confirm", "wallet_signature"]


class Event(BaseModel):
    """Something that happened: money arrived, the user spoke, a tap landed,
    or a wallet signature arrived."""

    model_config = ConfigDict(extra="forbid")

    type: EventType
    user_id: str
    # utterance
    text: str = ""
    # inflow
    inflow_id: str = ""
    amount: Decimal | None = None
    source_raw: str = ""
    # confirm tap
    confirm_id: str = ""
    # wallet that will own the Glider portfolio (base58). Terminal tests send
    # their ephemeral key; production sends the connected wallet address.
    wallet_address: str = ""
    # wallet signature settle
    flow_id: str = ""
    signed_tx: str = ""


@dataclass
class TurnResult:
    """Everything one turn produced, for the caller and for the tests.

    ``state`` is ``None`` only when the ledger could not be read, which is the one
    turn with no STATE to show because there was nothing to read it from.
    """

    state: HandlerState | None = None
    narration: str | None = None
    decision: dict[str, Any] | None = None
    receipt: Receipt | None = None
    confirm_id: str = ""
    audit: list[AuditRow] = field(default_factory=list)
    # True when the turn is silent by design rather than because Voice failed.
    quiet: bool = False
    # Allocate card (tap time): kind/amount/strategy/flow/sign payload. The
    # sign payload is the base64 Solana transaction the wallet must sign.
    card: dict[str, Any] | None = None
    # Settle result (wallet-signature time): ok/enrollment/funding/positions.
    invest: dict[str, Any] | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Orchestrator:
    """The only entrypoint into the three layers."""

    def __init__(
        self,
        *,
        store: LedgerStore | None = None,
        policy: Policy | None = None,
        rail: Rail | None = None,
        yield_rail: Rail | None = None,
        provider: Any = None,
        memory: Any = None,
        judge: Judge | None = None,
        clock: Callable[[], datetime] | None = None,
        go_token: str | None = None,
        audit_sink: Callable[[Event, TurnResult], Any] | None = None,
    ) -> None:
        self.store = store or InMemoryLedgerStore()
        self.policy = policy or Policy()
        self.rail: Rail = rail or InMemoryRail()
        self.yield_rail = yield_rail
        self.provider = provider
        self.memory = memory
        self.judge = judge
        self.clock = clock or _utcnow
        # Bearer token for the Go money/ledger host, used only by the invest
        # leg (Glider stage 1/2 via /api/v1/investments/*). None means the
        # invest path fails closed.
        self.go_token = go_token
        # Durable audit sink, injected by the API layer (which owns Postgres).
        # Called with the event and the finished TurnResult for every turn
        # that produced a receipt. Fail-open by contract: an audit outage is
        # logged, never allowed to break a settled money turn.
        self.audit_sink = audit_sink

    # -- the door ---------------------------------------------------------

    async def handle(self, event: Event) -> TurnResult:
        """Handle one event. The only public entry point for a turn.

        A ledger that cannot be read is a typed refusal, not a guess: the user is
        told the turn did not complete. An inflow is the exception and is
        re-raised, because money arriving must reach the rail as a retryable
        failure rather than as a chat reply the rail never sees.
        """
        try:
            result = await self._dispatch(event)
        except LedgerUnavailable:
            if event.type == "inflow":
                raise
            logger.error("ledger unavailable for %s; refusing the turn", event.user_id)
            return TurnResult(narration=UNAVAILABLE_LINE)
        await self._emit_audit(event, result)
        return result

    async def _emit_audit(self, event: Event, result: TurnResult) -> None:
        """Persist what the turn did, via the injected sink.

        Only turns with a receipt are emitted: a receipt is the fact that
        money was attempted, moved, or refused, and it is what the durable
        store must show later. The sink is fail-open — the receipt already
        lives in the ledger, so an audit outage degrades the paper trail
        without turning a settled turn into an error.
        """
        if self.audit_sink is None or result.receipt is None:
            return
        try:
            await self.audit_sink(event, result)
        except Exception:
            logger.exception(
                "audit sink failed; receipt %s kept in ledger only",
                result.receipt.id,
            )

    async def _dispatch(self, event: Event) -> TurnResult:
        ledger = await self.store.load(event.user_id)
        if ledger is None:
            ledger = new_ledger(event.user_id)
        if event.type == "inflow":
            return await self._handle_inflow(ledger, event)
        if event.type == "utterance":
            return await self._handle_utterance(ledger, event)
        if event.type == "wallet_signature":
            return await self._handle_wallet_signature(ledger, event)
        return await self._handle_confirm(ledger, event)

    async def handle_utterance(self, user_id: str, text: str) -> TurnResult:
        """Handle one thing the user said.

        The chat path calls this for a money turn, and for nothing else. A
        sentence is parsed into a structured action by code, judged by JEV, and
        only then handed to Hands.
        """
        return await self.handle(Event(type="utterance", user_id=user_id, text=text))

    async def handle_inflow(
        self,
        user_id: str,
        payment_id: str,
        amount: Decimal,
        source_raw: str = "",
    ) -> TurnResult:
        """Handle money arriving. Called by the rail webhook; reachable from
        chat only through the demo escape hatch (ALLOW_CHAT_INFLOW_SYNTH).

        ``payment_id`` is the rail's id for the payment and doubles as the
        idempotency key, so a webhook that is delivered twice splits once.
        """
        return await self.handle(
            Event(
                type="inflow",
                user_id=user_id,
                inflow_id=payment_id,
                amount=amount,
                source_raw=source_raw,
            )
        )

    async def handle_confirm(
        self, user_id: str, confirm_id: str, yes: bool
    ) -> TurnResult:
        """Settle a challenge the user tapped, by its Hands-issued id.

        ``yes=False`` is a decline: the challenge is closed and nothing moves.
        There is no other way in. A chat word never reaches this method, so
        "yes" typed into a message is just a message.
        """
        if yes:
            return await self.handle(
                Event(type="confirm", user_id=user_id, confirm_id=confirm_id)
            )
        # A decline goes through the ledger too, so it needs the same typed
        # refusal as the rest: without this a ledger outage answered a tap with a
        # 500 (and leaked the exception text down the SSE stream).
        try:
            return await self._decline_challenge(user_id, confirm_id)
        except LedgerUnavailable:
            logger.error("ledger unavailable for %s; refusing the decline", user_id)
            return TurnResult(narration=UNAVAILABLE_LINE)

    # -- inflow -----------------------------------------------------------

    async def _handle_inflow(self, ledger: Ledger, event: Event) -> TurnResult:
        """Classify, split, audit, then narrate a receipt.

        Judgment classifies because classification is a judgement. The split
        itself is arithmetic and happens regardless of what Judgment said, which
        is why killing the model cannot stop a payday.
        """
        if event.amount is None:
            raise ValueError("an inflow event needs an amount")
        audit = AuditLog()
        inflow_id = event.inflow_id or f"inflow_{self.clock().timestamp()}"

        # Judgment sees the inflow before it is split, so the classification is
        # made on what arrived rather than on what it became.
        provisional = ledger.model_copy(deep=True)
        provisional.pending_inflow = PendingInflow(
            id=inflow_id, amount=event.amount, source_raw=event.source_raw
        )
        state = build_state(ledger=provisional, policy=self.policy, now=self.clock())
        decision = await self._decide(state, provisional, "")

        outcome = await split_inflow(
            store=self.store,
            ledger=ledger,
            inflow_id=inflow_id,
            amount=event.amount,
            source_raw=event.source_raw,
            classified_as=decision.inflow_class,
            at=self.clock(),
        )
        audit.record(
            audit_row_for_split(
                user_id=ledger.user_id,
                receipt=outcome.receipt,
                decision_id=decision.id,
            )
        )

        state = build_state(
            ledger=ledger,
            policy=self.policy,
            decision=decision.model_dump(mode="json"),
            execution=self._execution_from(outcome.receipt),
            now=self.clock(),
        )
        narration = await self._speak(state)

        return TurnResult(
            state=state,
            narration=narration,
            decision=state.decision,
            receipt=outcome.receipt,
            audit=audit.rows,
        )

    # -- utterance --------------------------------------------------------

    async def _handle_utterance(self, ledger: Ledger, event: Event) -> TurnResult:
        """Parse, decide, then act, ask, or stay quiet. Voice speaks last."""
        audit = AuditLog()
        action = parse_transfer_utterance(event.text)
        if action is not None and action.source != "user":
            # Structurally unreachable today, and it stays that way: anything not
            # built from the user's own words is not an action.
            action = None
        if action is None:
            # The diversified stock sleeve. Single-name tickers parse to None
            # here (a later verb), so Judgment asks instead of investing.
            action = parse_invest_utterance(event.text)
            if action is not None and action.source != "user":
                action = None

        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            now=self.clock(),
        )
        decision = await self._decide(state, ledger, event.text)
        # The decision is written onto STATE before Hands is asked to execute,
        # because Hands re-reads it there and refuses a STATE with no verdict.
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            now=self.clock(),
        )

        execution: Execution | None = None
        receipt: Receipt | None = None
        confirm_id = ""

        if (
            decision.next_mode == "act"
            and decision.action_choice in ("allow", "allow_smaller")
            and (action is None or action.type != "invest")
        ):
            outcome = await self._execute(ledger, state, decision, action)
            if outcome is not None:
                receipt = outcome.receipt
                audit.rows.extend(outcome.audit)
                ledger = outcome.ledger
                execution = self._execution_from(receipt)

        if execution is None and action is not None:
            # Invest never executes from an utterance: the tap authorises the
            # money and the wallet signature authorises the chain write. An
            # "act" verdict stages the same challenge as "ask".
            if decision.action_choice in (
                "allow",
                "allow_smaller",
            ) and (
                decision.next_mode == "ask"
                or (action.type == "invest" and decision.next_mode == "act")
            ):
                confirm_id = await self._create_challenge(ledger, decision, action)
                decision = decision.model_copy(update={"confirm_id": confirm_id})
            elif decision.action_choice in ("deny", "defer"):
                # A refusal is a result. It gets a rejected receipt so the trail
                # shows an order arrived and was turned down.
                refusal = await record_refusal(
                    store=self.store,
                    ledger=ledger,
                    action=action,
                    reasons=list(decision.reasons),
                    decision_id=decision.id,
                    at=self.clock(),
                )
                receipt = refusal.receipt
                audit.rows.extend(refusal.audit)
                ledger = refusal.ledger

        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )

        if decision.next_mode == "stay_quiet" and execution is None:
            return TurnResult(
                state=state, decision=state.decision, quiet=True, audit=audit.rows
            )

        narration = await self._speak(state, utterance=event.text)
        return TurnResult(
            state=state,
            narration=narration,
            decision=state.decision,
            receipt=receipt,
            confirm_id=confirm_id,
            audit=audit.rows,
        )

    # -- confirm ----------------------------------------------------------

    async def _handle_confirm(self, ledger: Ledger, event: Event) -> TurnResult:
        """Execute the exact action a confirm_id was issued for.

        The challenge, not the sentence, is the authorisation: the amount, the
        destination, the sleeve and the user come from what Hands stored when it
        asked. Settle only if all match, one use, and a decline burns it.
        """
        audit = AuditLog()
        now = self.clock()
        challenge = ledger.challenges.get(event.confirm_id)

        if challenge is None or not challenge.is_open(now):
            reason = "NO_SUCH_CHALLENGE" if challenge is None else "CHALLENGE_EXPIRED"
            rejected: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                reasons=[reason],
                detail="that confirmation does not match anything open",
            )
            ledger.remember_receipt(rejected)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": [reason],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=rejected,
                audit=audit.rows,
            )

        if (
            challenge is not None
            and challenge.is_open(now)
            and challenge.user_id
            and challenge.user_id != ledger.user_id
        ):
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge

        if challenge.action == "invest":
            # The tap approves the money; the wallet signature (a separate
            # event) approves the chain write. Stage 1 runs here and returns
            # the transaction the wallet must sign.
            return await self._handle_confirm_invest(ledger, event, challenge)

        action = ProposedAction(
            type=_action_type(challenge.action),
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        # The challenge already carries the user's confirmation, so the decision
        # that authorises the movement is rebuilt from it. No second JEV call is
        # made and no model is consulted.
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        stored_destination = (
            challenge.destination or challenge.counterparty or challenge.sleeve
        )
        action_destination = action.counterparty or action.sleeve
        if challenge.user_id and challenge.user_id != ledger.user_id:
            bound = False
        else:
            bound = (
                money(action.amount or 0) == money(challenge.amount)
                and action_destination == stored_destination
                and money(action.amount or 0) > 0
            )
        if not bound:
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge
            mismatched: Receipt = Receipt(
                id=f"rcpt_rejected_{event.confirm_id or 'missing'}",
                at=now,
                status="rejected",
                action="confirm",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                reasons=["CHALLENGE_MISMATCH"],
                detail="that confirmation does not match the stored challenge",
            )
            ledger.remember_receipt(mismatched)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                decision={
                    "id": "",
                    "next_mode": "ask",
                    "action_choice": "deny",
                    "reasons": ["CHALLENGE_MISMATCH"],
                },
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=mismatched,
                audit=audit.rows,
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            now=now,
        )
        outcome = await self._execute(ledger, state, decision, action)
        receipt = outcome.receipt if outcome is not None else None
        if outcome is not None:
            audit.rows.extend(outcome.audit)
            ledger = outcome.ledger

        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            audit=audit.rows,
        )

    async def handle_wallet_signature(
        self, user_id: str, flow_id: str, signed_tx: str
    ) -> TurnResult:
        """Settle a prepared allocation with the wallet's signature.

        The second half of the invest leg: the tap approved the money, this
        approves the chain write. The pending binding is re-checked and the
        savings sleeve moves only after the host reports funding submitted.
        """
        return await self.handle(
            Event(
                type="wallet_signature",
                user_id=user_id,
                flow_id=flow_id,
                signed_tx=signed_tx,
            )
        )

    def _invest_calls(self) -> dict[str, Any] | None:
        """Bound Go-host calls for the invest leg, or None (fail closed)."""
        token = self.go_token
        if not token:
            return None
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()

        async def list_strategies() -> dict[str, Any]:
            return await client.list_investment_strategies(token, status="active")

        async def get_owner() -> dict[str, Any]:
            return await client.get_investment_owner(token)

        async def prepare_call(payload: dict[str, Any]) -> dict[str, Any]:
            tok = payload.pop("confirmation_token", None)
            return await client.prepare_user_enroll(
                token, payload, confirmation_token=tok
            )

        async def complete_call(payload: dict[str, Any]) -> dict[str, Any]:
            tok = payload.pop("confirmation_token", None)
            return await client.complete_user_enroll(
                token, payload, confirmation_token=tok
            )

        return {
            "list_strategies": list_strategies,
            "get_owner": get_owner,
            "prepare_call": prepare_call,
            "complete_call": complete_call,
        }

    async def _handle_confirm_invest(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle an invest tap: run Glider stage 1 and return the sign card."""
        from miriam_agent.hands.ledger import money as _money

        audit = AuditLog()
        now = self.clock()
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type="invest",
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        calls = self._invest_calls()
        if calls is None:
            receipt = Receipt(
                id=f"rcpt_rejected_{challenge.id}",
                at=now,
                status="rejected",
                action="invest_prepare",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                reasons=["GLIDER_UNREACHABLE"],
                detail="no Go host token on this path; nothing moved",
            )
            ledger.remember_receipt(receipt)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                proposed_action=action,
                decision=decision.model_dump(mode="json"),
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=receipt,
                confirm_id=challenge.id,
                audit=audit.rows,
            )

        owner_address = (event.wallet_address or "").strip() or None
        receipt, card, ledger = await prepare_allocate(
            store=self.store,
            ledger=ledger,
            user_id=event.user_id,
            token=self.go_token or "",
            amount=_money(challenge.amount),
            source="savings" if challenge.sleeve == INVEST_SLEEVE else challenge.sleeve,
            decision_id=challenge.decision_id,
            owner_address=owner_address,
            list_strategies=calls["list_strategies"],
            get_owner=calls["get_owner"],
            prepare_call=calls["prepare_call"],
            at=now,
        )
        # prepare_allocate only saves on paths that change provider state, so
        # persist here as well: the consumed challenge and the rejected receipt
        # must survive the request even when nothing moved.
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            card=card,
            audit=audit.rows,
        )

    async def _handle_wallet_signature(
        self, ledger: Ledger, event: Event
    ) -> TurnResult:
        """Settle a prepared allocation: run Glider stage 2 and fund."""
        audit = AuditLog()
        now = self.clock()
        calls = self._invest_calls()
        if calls is None:
            receipt = Receipt(
                id=f"rcpt_rejected_{(event.flow_id or 'missing')}",
                at=now,
                status="rejected",
                action="invest_settle",
                currency=ledger.currency,
                decision_id="",
                reasons=["GLIDER_UNREACHABLE"],
                detail="no Go host token on this path; nothing moved",
            )
            ledger.remember_receipt(receipt)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=receipt,
                audit=audit.rows,
            )
        receipt, result, ledger = await settle_allocate(
            store=self.store,
            ledger=ledger,
            user_id=event.user_id,
            flow_id=(event.flow_id or "").strip(),
            signed_tx=event.signed_tx or "",
            complete_call=calls["complete_call"],
            at=now,
        )
        # settle_allocate only saves on paths that change provider state, so
        # persist here as well: rejections must land in the audit trail even
        # when the sleeve did not move.
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            decision={
                "id": "",
                "next_mode": "act",
                "action_choice": "allow" if result.get("ok") else "deny",
                "reasons": result.get("reasons", []),
            },
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            invest=result,
            audit=audit.rows,
        )

    # -- shared steps -----------------------------------------------------

    async def _decline_challenge(self, user_id: str, confirm_id: str) -> TurnResult:
        """Close a challenge the user said no to. Nothing moves, ever."""
        ledger = await self.store.load(user_id) or new_ledger(user_id)
        now = self.clock()
        challenge = ledger.challenges.get(confirm_id)
        existing = {**ledger.sleeves}
        if challenge is not None and challenge.is_open(now):
            challenge.status = "consumed"
            ledger.challenges[challenge.id] = challenge

        receipt = Receipt(
            id=f"rcpt_declined_{confirm_id or 'missing'}",
            at=now,
            status="rejected",
            action="decline",
            currency=ledger.currency,
            amount=challenge.amount if challenge is not None else None,
            counterparty=challenge.counterparty if challenge is not None else "",
            reasons=["USER_DECLINED"],
            sleeves_before={k: str(v) for k, v in existing.items()},
            sleeves_after={k: str(v) for k, v in ledger.sleeves.items()},
            detail="the user declined; nothing moved",
        )
        ledger.remember_receipt(receipt)
        await self.store.save(ledger)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            decision={
                "id": challenge.decision_id if challenge is not None else "",
                "next_mode": "act",
                "action_choice": "none",
                "reasons": ["USER_DECLINED"],
                "confirm_id": "",
            },
            now=now,
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
        )

    async def _decide(
        self, state: HandlerState, ledger: Ledger, utterance: str
    ) -> Decision:
        return await decide(
            state=state,
            ledger=ledger,
            policy=self.policy,
            utterance=utterance,
            judge=self.judge,
            at=self.clock(),
        )

    async def _execute(
        self,
        ledger: Ledger,
        state: HandlerState,
        decision: Decision,
        action: ProposedAction | None,
    ) -> TransferOutcome | None:
        """Dispatch one authorised action to Hands.

        Hands re-checks the decision and the limits, so this function being
        wrong about authorisation cannot by itself move money.
        """
        if action is None:
            return None
        if action.type == "transfer":
            return await execute_transfer(
                store=self.store,
                ledger=ledger,
                state=state,
                policy=self.policy,
                rail=self.rail,
                at=self.clock(),
            )
        if action.type == "internal_move":
            return await move_between_sleeves(
                store=self.store,
                ledger=ledger,
                from_sleeve="spendable",
                to_sleeve=action.sleeve,
                amount=action.amount or Decimal("0"),
                policy=self.policy,
                action="internal_move",
                decision_id=decision.id,
                at=self.clock(),
            )
        if action.type in ("lock", "unlock"):
            return await move_between_sleeves(
                store=self.store,
                ledger=ledger,
                from_sleeve="spendable" if action.type == "lock" else "locked",
                to_sleeve="locked" if action.type == "lock" else "spendable",
                amount=action.amount or Decimal("0"),
                policy=self.policy,
                action=action.type,
                decision_id=decision.id,
                at=self.clock(),
            )
        # A purchase is an advice turn by the time it reaches a decision, so it
        # never arrives here with authority to move anything.
        return None

    async def yield_route(
        self, *, user_id: str, amount: Decimal, decision_id: str = ""
    ) -> Receipt:
        """Route money to the yield sleeve. Exposed for a scheduled caller."""
        ledger = await self.store.load(user_id) or new_ledger(user_id)
        outcome = await route_yield(
            store=self.store,
            ledger=ledger,
            amount=amount,
            policy=self.policy,
            yield_rail=self.yield_rail,
            decision_id=decision_id,
            at=self.clock(),
        )
        return outcome.receipt

    async def _create_challenge(
        self, ledger: Ledger, decision: Decision, action: ProposedAction
    ) -> str:
        """Stage the exact action for a tap, and return its confirm id."""
        now = self.clock()
        amount = action.amount or Decimal("0")
        if decision.suggested_amount is not None and amount > decision.suggested_amount:
            amount = decision.suggested_amount
        counterparty = action.counterparty
        sleeve = action.sleeve
        destination = counterparty or sleeve
        challenge = Challenge(
            id=f"confirm_{decision.id[-8:]}",
            user_id=ledger.user_id,
            action=action.type,
            amount=money(amount),
            counterparty=counterparty,
            destination=destination,
            sleeve=sleeve,
            decision_id=decision.id,
            reasons=list(decision.reasons),
            created_at=now,
            expires_at=now + timedelta(minutes=CHALLENGE_TTL_MINUTES),
            meta={"strategy": "rail-stock-sleeve"} if action.type == "invest" else {},
        )
        ledger.challenges[challenge.id] = challenge
        await self.store.save(ledger)
        return challenge.id

    @staticmethod
    def _execution_from(receipt: Receipt | None) -> Execution | None:
        if receipt is None:
            return None
        return Execution(
            receipt_id=receipt.id,
            status=receipt.status,
            action=receipt.action,
            amount=receipt.amount,
            counterparty=receipt.counterparty,
            sleeve=receipt.sleeve,
            rail_reference=receipt.rail_reference,
            idempotent_replay=receipt.idempotent_replay,
            at=receipt.at,
        )

    async def _speak(self, state: HandlerState, *, utterance: str = "") -> str | None:
        """Narrate STATE. Returns ``None`` when Voice must not be called.

        Voice is the last thing that runs and it is optional: a dead provider
        yields the deterministic line, and a ``stay_quiet`` turn with nothing
        executed is not narrated at all.
        """
        facts = await read_facts(state.user_id, utterance or None, memory=self.memory)
        try:
            message = await speak(
                state=state,
                utterance=utterance,
                provider=self.provider,
                facts=facts,
            )
        except Exception as exc:  # noqa: BLE001 - narration never costs a movement
            logger.warning("voice failed, falling back to STATE only: %s", exc)
            return deterministic_message(state)
        return message.text if message is not None else None


def _action_type(value: str) -> Any:
    """Narrow a stored challenge action back to the ProposedAction vocabulary."""
    return (
        value
        if value in ("transfer", "purchase", "lock", "unlock", "invest")
        else "none"
    )


# ---------------------------------------------------------------------------
# Which entrypoint owns this turn
# ---------------------------------------------------------------------------

TurnRoute = Literal["orchestrator", "agent"]

# Words that ask for money to move. Deliberately the same vocabulary the parser
# in hands/transfer.py understands, so a turn is never routed here for a phrase
# the parser would then ignore.
_MOVE_WORDS = frozenset(
    {
        "send",
        "transfer",
        "pay",
        "give",
        "move",
        "split",
        "lock",
        "unlock",
        "freeze",
        "release",
        "reserve",
    }
)

# Words that ask for money to move into the stock sleeve. Deliberately the
# same vocabulary the parser in hands/invest.py understands, so a turn is
# never routed here for a phrase the parser would then ignore. Single-name
# tickers are not here: they are a later verb, not the sleeve.
_INVEST_WORDS = frozenset({"invest", "stocks", "sleeve"})

# Frames that ask whether something is affordable. A judgement question, not a
# statement of fact, so it goes to Judgment rather than to the answer path.
_INVEST_WORDS = frozenset({"invest", "stocks", "sleeve"})
_AFFORD_FRAMES = (
    "can i afford",
    "can i buy",
    "can i get",
    "could i afford",
    "could i buy",
    "should i buy",
    "do i have enough",
    "is it affordable",
    "afford",
)

# Track changes. The track is the income split, so changing it is a money
# decision and never a chat answer.
_TRACK_FRAMES = (
    "change my track",
    "change the track",
    "set my track",
    "set the track",
    "switch to ",
    "change my split",
    "update my split",
    "change my ratios",
)

# Inflow alerts pasted into chat, e.g. "Credit alert: NGN420,000.00 from ACME".
# The authoritative inflow path is the rail webhook; this is for a forwarded
# alert. The derived id is stable, so re-pasting the same alert splits once.
_INFLOW_FRAMES = (
    "credit alert",
    "debit alert",
    "you received",
    "you have received",
    "credited with",
    "inflow of",
    "payment received",
    "salary alert",
    # Payday language typed by hand. The rail webhook is authoritative for real
    # money; a pasted "i just got paid 100" splits the same 70/30 book so the
    # demo loop and manual top-ups behave like the rail.
    "got paid",
    "payday",
)

_AMOUNT_HINT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?", re.UNICODE)


def looks_like_inflow_alert(text: str) -> bool:
    """Whether a message is a forwarded inflow alert rather than an order.

    A message that also names a movement verb is an instruction, not an alert,
    so it belongs to the utterance path.
    """
    lowered = (text or "").casefold()
    if not any(frame in lowered for frame in _INFLOW_FRAMES):
        return False
    words = set(re.findall(r"[a-z']+", lowered))
    return not (words & _MOVE_WORDS)


# The classifier reads the user's text only. It never calls a model: which layer
# owns a turn must not be a judgement a model can be talked out of.
def classify_turn(text: str, *, has_confirm_id: bool = False) -> TurnRoute:
    """Decide whether a turn is a money turn.

    Money turns belong to the orchestrator, where the only path is
    Hands -> Judgment -> Hands -> Voice. Everything else is answerable and goes
    to the agent loop, which cannot move money at all.
    """
    if has_confirm_id:
        return "orchestrator"

    lowered = (text or "").strip().casefold()
    if not lowered:
        return "agent"

    if any(frame in lowered for frame in _TRACK_FRAMES):
        return "orchestrator"
    if any(frame in lowered for frame in _AFFORD_FRAMES):
        return "orchestrator"

    words = set(re.findall(r"[a-z']+", lowered))
    has_amount = bool(_AMOUNT_HINT_RE.search(lowered))

    if looks_like_inflow_alert(lowered) and has_amount:
        return "orchestrator"
    # A movement verb with an amount is a money turn. A movement verb without one
    # ("send it to her") is not parseable, so the agent answers it and the user
    # gets a question instead of a silent no-op.
    if has_amount and words & _MOVE_WORDS:
        return "orchestrator"
    if has_amount and words & _INVEST_WORDS:
        return "orchestrator"
    if "rent" in words and has_amount and words & {"reserve", "protect", "cover"}:
        return "orchestrator"
    return "agent"


def inflow_id_for_alert(text: str) -> str:
    """A stable id for an inflow alert pasted into chat.

    Two pastes of the same alert must split once, so the id is derived from the
    alert rather than minted per turn. The rail webhook path uses the rail's own
    payment id instead.
    """
    digest = hashlib.sha256(" ".join((text or "").split()).casefold().encode())
    return f"alert_{digest.hexdigest()[:16]}"


__all__ = [
    "CHALLENGE_TTL_MINUTES",
    "Event",
    "Orchestrator",
    "TurnResult",
    "TurnRoute",
    "classify_turn",
    "inflow_id_for_alert",
    "looks_like_inflow_alert",
]
