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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from miriam_agent.hands.audit import AuditLog, Receipt
from miriam_agent.hands.funding import extract_otp as _extract_funding_otp
from miriam_agent.hands.funding import parse_funding_utterance, parse_offramp_utterance
from miriam_agent.hands.invest import (
    parse_invest_utterance,
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
from miriam_agent.hands.orders import (
    parse_order_utterance,
    parse_rebalance_utterance,
)
from miriam_agent.hands.save_rule import (
    parse_save_rule_utterance,
    save_rule_binding,
)
from miriam_agent.hands.settle_cards import CardSettlementMixin
from miriam_agent.hands.settle_funding import (
    FundingSettlementMixin,
    _open_paj_otp_challenge,
)
from miriam_agent.hands.settle_legs import LegSettlementMixin
from miriam_agent.hands.settlement import Event, SettlementMixin, TurnResult
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Orchestrator(
    SettlementMixin, LegSettlementMixin, FundingSettlementMixin, CardSettlementMixin
):
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
        cards_enabled: bool = False,
        card_channels: tuple[str, ...] | list[str] = ("imessage",),
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
        # Live Face ID cards (Go card <-> Miriam challenge join). Off by
        # default: when off, turns are byte-identical to the legacy narration
        # and zero Go card calls are made. When on, only allowlisted channels
        # mint, best-effort, after the challenge is staged.
        self.cards_enabled = cards_enabled
        self.card_channels = tuple(card_channels)
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
        if event.type == "confirm" and event.provenance:
            # Face ID provenance rides on the audit rows, never on the
            # decision: the trail shows which factor settled the challenge.
            result = self._tag_provenance(result, event.provenance)
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

    async def handle_utterance(
        self, user_id: str, text: str, *, channel: str = "", thread_id: str = ""
    ) -> TurnResult:
        """Handle one thing the user said.

        The chat path calls this for a money turn, and for nothing else. A
        sentence is parsed into a structured action by code, judged by JEV, and
        only then handed to Hands. ``channel``/``thread_id`` let an allowlisted
        surface (iMessage) mint a Face ID card after the challenge is staged.
        """
        return await self.handle(
            Event(
                type="utterance",
                user_id=user_id,
                text=text,
                channel=channel,
                thread_id=thread_id,
            )
        )

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
        # NGN <-> crypto funding first: a crypto buy is an onramp, not a
        # purchase-advice turn. The funding parser only claims sentences with
        # a crypto/naira/top-up word, so grocery buys fall through below.
        action = parse_funding_utterance(event.text)
        if action is not None and action.source != "user":
            # Structurally unreachable today, and it stays that way: anything not
            # built from the user's own words is not an action.
            action = None
        if action is None:
            # Funds-OUT is app-only: parse so it stages an envelope, never a rail.
            action = parse_offramp_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            # Single-name sleeve transactions BEFORE generic transfer: the
            # transfer parser claims any "buy ... <amount>" as purchase
            # advice, which would shadow "buy 50 NVDAx" or "buy me some
            # apple stock". The order parser only claims sentences with a
            # ticker/company symbol, so grocery buys still fall through.
            action = parse_order_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            action = parse_rebalance_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            action = parse_transfer_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            # The diversified stock sleeve. Single-name tickers parse to None
            # here (a later verb), so Judgment asks instead of investing.
            action = parse_invest_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            # Save-rule updates ("park 15% of this inflow"). Parsed last so
            # the movement verbs above keep priority; the trigger words are
            # tight enough that stash moves never land here.
            action = parse_save_rule_utterance(event.text)
            if action is not None and action.source != "user":
                action = None
        if action is None:
            # An OTP typed as the next message settles a pending Paj challenge.
            # The challenge (30-min TTL), not the chat word, is the authority.
            otp = _extract_funding_otp(event.text)
            if otp is not None:
                pending = _open_paj_otp_challenge(ledger, self.clock())
                if pending is not None:
                    return await self._handle_funding_otp(ledger, event, pending, otp)

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
        card_action_id = ""

        if (
            decision.next_mode == "act"
            and decision.action_choice in ("allow", "allow_smaller")
            and (
                action is None
                or action.type
                not in (
                    "invest",
                    "order",
                    "rebalance",
                    "onramp",
                    "offramp",
                    "set_allocation",
                    "pause",
                    "resume",
                    "save_rule",
                )
            )
        ):
            outcome = await self._execute(ledger, state, decision, action)
            if outcome is not None:
                receipt = outcome.receipt
                for row in outcome.audit:
                    audit.record(row)
                ledger = outcome.ledger
                execution = self._execution_from(receipt)

        if execution is None and action is not None:
            # Invest, orders, rebalance and save-rule changes never execute
            # from an utterance: the tap authorises the money
            # (orders/rebalance/save-rule are server-signed, so no wallet
            # signature follows). An "act" verdict stages the same challenge
            # as "ask".
            if decision.action_choice in (
                "allow",
                "allow_smaller",
            ) and (
                decision.next_mode == "ask"
                or (
                    action.type
                    in (
                        "invest",
                        "order",
                        "rebalance",
                        "onramp",
                        "offramp",
                        "set_allocation",
                        "pause",
                        "resume",
                        "save_rule",
                    )
                    and decision.next_mode == "act"
                )
            ):
                confirm_id = await self._create_challenge(ledger, decision, action)
                decision = decision.model_copy(update={"confirm_id": confirm_id})
                card_action_id = await self._maybe_mint_card(
                    ledger,
                    confirm_id,
                    channel=event.channel,
                    thread_id=event.thread_id,
                )
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
                for row in refusal.audit:
                    audit.record(row)
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
        if card_action_id and narration:
            # The live card is in the transcript; the chat fallback stays
            # valid either way (the CONFIRM: line above is untouched).
            narration = (
                f"{narration} Approve with Face ID in Messages, "
                f"or reply confirm {confirm_id}."
            )
        return TurnResult(
            state=state,
            narration=narration,
            decision=state.decision,
            receipt=receipt,
            confirm_id=confirm_id,
            audit=audit.rows,
            card_action_id=card_action_id,
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
        if challenge is not None and (challenge.card_action_id or "").strip():
            # The chat tap won: the live card must show the same ending.
            challenge.card_state = "rejected"
            await self._mark_card_state(
                challenge.card_action_id.strip(),
                "rejected",
                "declined; nothing moved",
            )
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
        meta: dict[str, str] = {}
        if action.type == "invest":
            meta = {"strategy": "rail-stock-sleeve"}
        elif action.type == "order":
            meta = {
                "strategy": "rail-stock-sleeve",
                "side": action.side or "buy",
                "symbol": counterparty,
            }
        elif action.type in ("rebalance", "pause", "resume"):
            meta = {"strategy": "rail-stock-sleeve"}
        elif action.type == "onramp":
            meta = {
                "side": "onramp",
                "symbol": counterparty or "USDC",
                "provider": "paj",
                "currency": "NGN",
            }
        elif action.type == "offramp":
            meta = {
                "side": "offramp",
                "currency": "NGN",
                "provider": "paj",
            }
        elif action.type == "save_rule":
            # The binding is re-derived from the user's own words: a
            # percentage ("park 15%") or a flat amount per inflow.
            binding = save_rule_binding(action.raw)
            if binding is not None:
                meta = {binding[0]: binding[1]}
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
            meta=meta,
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


# Card keys that may ride on STATE.execution.funding. A whitelist, so a card
# can never smuggle a figure Voice did not earn from the Go result.


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

# Words that ask for a single-name sleeve transaction or a sleeve rebalance.
# Deliberately the same vocabulary hands/orders.py understands, so a turn is
# never routed here for a phrase the parser would then ignore. A bare ticker
# alone is not enough: the parser needs a side, an amount, and a symbol.
_ORDER_WORDS = frozenset({"buy", "sell", "long", "short", "trim", "dump"})
_REBALANCE_WORDS = frozenset({"rebalance", "re-balance", "rebal"})
_SLEEVE_NOUNS = frozenset({"sleeve", "stocks", "stock", "portfolio"})

# NGN <-> crypto funding. "buy"/"sell" plus a crypto/naira word routes to the
# orchestrator when an amount is present, mirroring hands/funding.py.
_FUND_WORDS = frozenset({"buy", "sell"})
_CRYPTO_WORDS = frozenset(
    {
        "bitcoin",
        "btc",
        "usdt",
        "usdc",
        "crypto",
        "naira",
        "ngn",
        "top up",
        "top-up",
        "fund",
        "add money",
        "onramp",
        "offramp",
        "cash out",
        "cashout",
    }
)

# Frames that ask whether something is affordable. A judgement question, not a
# statement of fact, so it goes to Judgment rather than to the answer path.
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
    # A buy/sell of a ticker, or a sleeve rebalance, is a money turn. Both need
    # an amount (or the sleeve word) to be parseable; a bare ticker with no verb
    # is a question and goes to the agent.
    if has_amount and ((words & _ORDER_WORDS) and words & _SLEEVE_NOUNS):
        return "orchestrator"
    if words & _REBALANCE_WORDS and words & _SLEEVE_NOUNS:
        return "orchestrator"
    if "rent" in words and has_amount and words & {"reserve", "protect", "cover"}:
        return "orchestrator"
    # NGN <-> crypto funding: buy/sell + crypto/naira word + amount.
    if (
        has_amount
        and words & _FUND_WORDS
        and (words & _CRYPTO_WORDS or "top up" in lowered or "add money" in lowered)
    ):
        return "orchestrator"
    # A bare OTP (4-8 digits) answers a pending Paj challenge.
    if re.fullmatch(r"\s*\d{4,8}\s*", text or ""):
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
