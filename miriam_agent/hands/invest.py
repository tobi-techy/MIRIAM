"""Layer 1 - HANDS. The Glider stock-sleeve invest leg.

Deterministic code only. No LLM, no JEV.

The product default is a diversified stock sleeve (Rail Stock Sleeve on
Solana), never a single-name buy. Single-name is an explicit later verb with
a cap, so a ticker in an invest sentence parses to *no action* today.

Money never leaves the spend pot for investing without a confirm card: invest
actions draw on the ``savings`` (stash) sleeve only, and every invest movement
needs a Hands-issued ``confirm_id`` tap first.

Two-phase settle, because the chain write needs the user's wallet:

1. tap (``prepare_allocate``) -- the challenge is consumed, Go stage 1 is run
   (obtain the staged confirmation, replay the exact payload with its token),
   and the base64 Solana transaction comes back for the wallet to sign. The
   flow binding is recorded as ``PendingInvest``.
2. wallet signature (``settle_allocate``) -- the binding is re-checked
   (flow + amount + strategy + owner, byte-for-byte round trip), Go stage 2
   runs the same obtain-and-replay, automation starts, funding settles, and
   only then is the savings sleeve debited.

Fail closed everywhere: no sleeve, no owner wallet, a simulated backend, or
empty positions all surface as explicit refusals. Nothing is invented, and no
silent simulation ever reaches the demo path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from miriam_agent.hands.audit import Receipt, sleeves_snapshot
from miriam_agent.hands.invest_funding import (
    INVEST_SLEEVE,
    _extract_funding,
    _fund_existing,
    _id,
    _needs_review_card,
    _obtain_and_replay,
    _sleeve_from_catalogue,
)
from miriam_agent.hands.ledger import (
    Ledger,
    LedgerStore,
    Movement,
    PendingInvest,
    money,
)
from miriam_agent.hands.limits import Policy, check_amount
from miriam_agent.hands.state import ProposedAction

logger = logging.getLogger(__name__)

# The sleeve all invest movements draw on. Never the spend pot.
INVEST_SOURCE = "savings"

_INVEST_WORDS = (
    "invest",
    "stocks",
    "stock sleeve",
    "sleeve",
    "into stocks",
    "stock market",
)
# A bare ticker ("buy NVDA", "TSLA 50") is the later single-name verb, not the
# sleeve. Parsing it today would invent a product that does not exist.
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
_SINGLE_NAME_WORDS = ("buy ", "sell ", "ticker", "share of", "shares of")

# Confirm cards expire fast: the Glider stage-1 transaction carries a recent
# blockhash, so sign-and-submit must happen promptly.
ALLOCATE_TTL_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_invest_utterance(text: str) -> ProposedAction | None:
    """Turn an invest sentence into a sleeve action, with regex.

    Returns ``None`` when the sentence is not a diversified-sleeve invest, or
    when it names a single ticker (the later verb). A model is never asked.
    """
    from miriam_agent.hands.transfer import parse_amount

    lowered = (text or "").casefold()
    if not lowered.strip():
        return None
    if not any(word in lowered for word in _INVEST_WORDS):
        return None
    if any(word in lowered for word in _SINGLE_NAME_WORDS):
        return None
    amount = parse_amount(lowered)
    if amount is None:
        return None
    return ProposedAction(
        type="invest",
        amount=amount,
        counterparty="Rail Stock Sleeve",
        sleeve=INVEST_SLEEVE,
        raw=text,
        source="user",
    )


def invest_amount_ok(policy: Policy, amount: Decimal) -> list[str]:
    """Reason codes for an invest amount breach. Empty means within limits."""
    return check_amount(policy, money(amount))


async def prepare_allocate(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    token: str,
    amount: Decimal,
    source: str = INVEST_SOURCE,
    decision_id: str = "",
    owner_address: str | None = None,
    list_strategies: Callable[..., Awaitable[dict[str, Any]]],
    get_owner: Callable[..., Awaitable[dict[str, Any]]],
    prepare_call: Callable[..., Awaitable[dict[str, Any]]],
    fund_call: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    read_stash: Callable[..., Awaitable[Decimal]] | None = None,
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any] | None, Ledger]:
    """Run Glider stage 1 after the tap and return the sign payload + card.

    Returns ``(receipt, card, ledger)``. ``card`` is ``None`` exactly when the
    receipt is rejected; a rejected receipt changes no balance, ever.
    """
    from miriam_agent.tools.glider_sleeve import (
        SOLANA_CHAIN_ID,
        owner_account_for,
        parse_caip10,
    )

    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)
    amount = money(amount)

    def _reject(reasons: list[str], detail: str) -> tuple[Receipt, None, Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="invest_prepare",
            currency=ledger.currency,
            amount=amount,
            counterparty="Rail Stock Sleeve",
            sleeve=INVEST_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"invest-prepare:{decision_id}:{amount}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, None, ledger

    if source != INVEST_SOURCE:
        return _reject(
            ["BAD_INVEST_SOURCE"], "investing draws on the stash sleeve only"
        )
    if amount <= 0:
        return _reject(["BAD_AMOUNT"], "the allocate amount must be greater than zero")
    if decision_id:
        # A retap of an already-funded top-up replays the original receipt
        # without touching the balance check, the catalogue, the owner
        # lookup, or any provider call. The key binds decision + amount, so a
        # fresh tap (new decision_id) or a different amount never replays.
        # Empty decision ids never replay: the key would not be unique.
        prior_topup = ledger.receipt_for(f"invest-topup:{decision_id}:{amount}")
        if prior_topup is not None and prior_topup.status == "executed":
            replay = prior_topup.model_copy(update={"idempotent_replay": True})
            card = {
                "kind": "funded",
                "title": "FUNDED",
                "subtitle": "Glider · Rail Stock Sleeve",
                "primary": "Already added to the existing sleeve",
                "amount": (
                    f"{prior_topup.amount} {ledger.currency}"
                    if prior_topup.amount is not None
                    else f"{amount} {ledger.currency}"
                ),
                "source": "stash",
                "strategy_id": prior_topup.strategy_id,
                "enrollment_id": prior_topup.rail_reference,
                "idempotent_replay": True,
            }
            return replay, card, ledger
        if prior_topup is not None and prior_topup.status in ("parked", "noop"):
            # Terminal without a second provider call: remote may already
            # hold the money, so a retap replays the review card, never a
            # FUNDED card and never a re-fund. Mirrors the guard inside
            # _fund_existing, just earlier (no balance/catalogue/provider).
            replay = prior_topup.model_copy(update={"idempotent_replay": True})
            card = _needs_review_card(
                amount=(
                    prior_topup.amount if prior_topup.amount is not None else amount
                ),
                currency=ledger.currency,
                strategy_id=prior_topup.strategy_id,
                enrollment_id=prior_topup.rail_reference,
            )
            card["idempotent_replay"] = True
            return replay, card, ledger
    if amount > ledger.balance(INVEST_SLEEVE) and read_stash is not None:
        # Go holds the real USDC. The chat sleeve is a mirror and is often
        # still zero after a Naira buy credits stash directly. The sync is an
        # audited adjustment (own Movement), and `before` is refreshed so a
        # later `_reject` stays truthful: rejected always means the invest
        # itself moved nothing; the sync is recorded separately.
        try:
            live = money(await read_stash())
        except Exception as exc:  # noqa: BLE001 - an unread balance is a refusal
            logger.warning("invest prepare: stash balance unreadable: %s", exc)
            return _reject(
                ["BALANCE_UNREADABLE"],
                "the stash balance could not be read; nothing moved",
            )
        held = ledger.balance(INVEST_SLEEVE)
        if live > held:
            delta = money(live - held)
            ledger.credit(INVEST_SLEEVE, delta)
            ledger.record_movement(
                Movement(
                    kind="inflow",
                    amount=delta,
                    sleeve=INVEST_SLEEVE,
                    counterparty="Go stash",
                    category="stash_sync",
                    ref=decision_id,
                    at=timestamp,
                )
            )
            before = sleeves_snapshot(ledger.sleeves)
    if amount > ledger.balance(INVEST_SLEEVE):
        return _reject(
            ["OVER_BALANCE"],
            f"the stash holds {ledger.balance(INVEST_SLEEVE)}, "
            f"which is less than {amount}. If you just paid naira for an "
            f"onramp, wait for the Go stash credit then retry — this step "
            f"re-reads the live stash and auto-syncs before refusing.",
        )

    try:
        catalogue = await list_strategies()
        strategies = (
            catalogue.get("strategies", []) if isinstance(catalogue, dict) else []
        )
        sleeve = _sleeve_from_catalogue(
            strategies if isinstance(strategies, list) else []
        )
    except Exception as exc:  # noqa: BLE001 - a lookup failure is a business result
        logger.warning("invest prepare: catalogue unreadable: %s", exc)
        return _reject(
            ["GLIDER_UNREACHABLE"],
            "the stock sleeve catalogue could not be read; nothing moved",
        )
    if sleeve is None:
        if isinstance(catalogue, dict) and catalogue.get("partial"):
            return _reject(
                ["SLEEVE_MISSING"],
                "strategy catalogue partial; Rail Stock Sleeve not in the "
                "visible half, not necessarily unconfigured; retry, do not "
                "seed a duplicate or invent tickers",
            )
        return _reject(
            ["SLEEVE_MISSING"],
            "Rail Stock Sleeve is not configured yet; escalate, do not invent tickers",
        )
    from miriam_agent.integrations.go_client import investment_strategy_id

    strategy_id = investment_strategy_id(sleeve)
    glider_strategy_id = str(
        sleeve.get("glider_strategy_id") or sleeve.get("gliderStrategyId") or ""
    )
    if not strategy_id or not glider_strategy_id:
        return _reject(
            ["SLEEVE_UNBOUND"], "the sleeve has no live provider binding; nothing moved"
        )

    try:
        if owner_address:
            owner_account = owner_account_for(owner_address)
        else:
            owner_doc = await get_owner()
            if not isinstance(owner_doc, dict):
                raise ValueError("owner lookup returned no account document")
            owner_account = str(
                owner_doc.get("owner_account_id")
                or owner_doc.get("ownerAccountId")
                or ""
            )
            parsed_owner = parse_caip10(owner_account)
            if parsed_owner.get("namespace") != "solana":
                raise ValueError(
                    f"owner wallet is not a Solana account: {owner_account!r}"
                )
    except Exception as exc:  # noqa: BLE001 - surfaced below, never swallowed
        return _reject(
            ["NO_OWNER_WALLET"],
            f"no Solana owner wallet for this account ({exc}); nothing moved",
        )

    try:
        prepared = await _obtain_and_replay(
            prepare_call,
            {
                "strategy_id": strategy_id,
                "owner_account_id": owner_account,
                "amount_usd": float(amount),
                "source": "stash",
            },
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("invest prepare: stage 1 failed: %s", exc)
        return _reject(
            ["GLIDER_NOT_LIVE"],
            f"Glider enrollment did not start ({exc}); "
            "the card says so and nothing moved",
        )

    status = str(prepared.get("status") or "")
    if status == "AWAITING_CONFIRMATION":
        return _reject(
            ["STILL_STAGED"], "the provider stayed staged after replay; nothing moved"
        )
    if prepared.get("already_enrolled"):
        return await _fund_existing(
            store=store,
            ledger=ledger,
            amount=amount,
            strategy_id=strategy_id,
            decision_id=decision_id,
            fund_call=fund_call,
            prepared=prepared,
            before=before,
            timestamp=timestamp,
            reject=_reject,
        )
    if prepared.get("simulated") is True or prepared.get("live") is False:
        return _reject(
            ["GLIDER_NOT_LIVE"],
            "Glider is not live (simulation mode); no sign payload was issued",
        )
    sign_payload = str(
        prepared.get("sign_payload") or prepared.get("solanaTransaction") or ""
    )
    flow_id = str(prepared.get("flow_id") or prepared.get("flowId") or "")
    if not sign_payload or not flow_id:
        return _reject(
            ["NO_SIGN_PAYLOAD"], "Glider returned no transaction to sign; nothing moved"
        )

    binding = PendingInvest(
        flow_id=flow_id,
        confirm_id=decision_id,
        strategy_id=strategy_id,
        glider_strategy_id=glider_strategy_id,
        owner_account_id=owner_account,
        account_index=str(
            prepared.get("account_index") or prepared.get("accountIndex") or "0"
        ),
        agent_account_id=str(
            prepared.get("agent_account_id") or prepared.get("agentAccountId") or ""
        ),
        amount=str(amount),
        source="stash",
        status="pending",
        created_at=timestamp,
    )
    ledger.pending_invest[flow_id] = binding
    await store.save(ledger)

    chain_ids = (
        prepared.get("chain_ids") or prepared.get("chainIds") or [SOLANA_CHAIN_ID]
    )
    card: dict[str, Any] = {
        "kind": "allocate",
        "title": "ALLOCATE",
        "subtitle": "Glider \u00b7 Rail Stock Sleeve",
        "primary": "AAPLx 40 / NVDAx 30 / TSLAx 30",
        "amount": f"{amount} {ledger.currency}",
        "source": "stash",
        "cta": "Approve",
        "flow_id": flow_id,
        "strategy_id": strategy_id,
        "account_index": str(
            prepared.get("account_index") or prepared.get("accountIndex") or "0"
        ),
        "agent_account_id": str(
            prepared.get("agent_account_id") or prepared.get("agentAccountId") or ""
        ),
        "chain_ids": chain_ids,
        "sign_payload": sign_payload,
        "deposit_account_id": str(
            prepared.get("deposit_account_id") or prepared.get("depositAccountId") or ""
        ),
        "expires_in_sec": ALLOCATE_TTL_MINUTES * 60,
    }
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="queued",
        action="invest_prepare",
        currency=ledger.currency,
        amount=amount,
        counterparty="Rail Stock Sleeve",
        sleeve=INVEST_SLEEVE,
        decision_id=decision_id,
        idempotency_key=f"invest-prepare:{decision_id}:{amount}",
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        detail=f"tap approved; stage 1 prepared flow {flow_id}, "
        "awaiting wallet signature",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    return receipt, card, ledger


async def settle_allocate(
    *,
    store: LedgerStore,
    ledger: Ledger,
    user_id: str,
    flow_id: str,
    signed_tx: str,
    decision_id: str = "",
    complete_call: Callable[..., Awaitable[dict[str, Any]]],
    at: datetime | None = None,
) -> tuple[Receipt, dict[str, Any], Ledger]:
    """Run Glider stage 2 with the wallet signature and fund the sleeve.

    The pending binding is re-checked field by field; anything that does not
    match what the card showed is rejected and nothing moves. The savings
    sleeve is debited only after the host reports the funding submitted.
    ``rejected`` always means nothing moved anywhere; if Go funded remotely
    but the local debit refuses, the receipt is ``parked`` with
    ``LEDGER_DIVERGED`` for a human, and the key is terminal on retap.
    Returns ``(receipt, result, ledger)`` where result carries positions (or
    the indexing note) for the chart step.
    """
    timestamp = at if at is not None else _utcnow()
    before = sleeves_snapshot(ledger.sleeves)

    def _reject(
        reasons: list[str], detail: str
    ) -> tuple[Receipt, dict[str, Any], Ledger]:
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="rejected",
            action="invest_settle",
            currency=ledger.currency,
            counterparty="Rail Stock Sleeve",
            sleeve=INVEST_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"invest-settle:{flow_id}",
            reasons=reasons,
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            detail=detail,
        )
        ledger.remember_receipt(receipt)
        return receipt, {"ok": False, "reasons": reasons, "detail": detail}, ledger

    binding = ledger.pending_invest.get(flow_id)
    if binding is None:
        return _reject(
            ["NO_SUCH_FLOW"], "that confirmation does not match anything open"
        )
    prior = ledger.receipt_for(f"invest-settle:{flow_id}")
    if prior is not None and prior.status == "executed":
        replay = prior.model_copy(update={"idempotent_replay": True})
        return replay, {"ok": True, "idempotent_replay": True}, ledger
    if prior is not None and prior.status in ("parked", "noop"):
        # Remote may already hold the money; never re-run stage 2 under
        # the same flow key. Hand the parked receipt back for reconciliation.
        replay = prior.model_copy(update={"idempotent_replay": True})
        return (
            replay,
            {
                "ok": False,
                "reasons": list(prior.reasons or ["LEDGER_DIVERGED"]),
                "detail": prior.detail,
                "idempotent_replay": True,
            },
            ledger,
        )
    if binding.status != "pending":
        return _reject(["FLOW_CLOSED"], "that flow already settled")
    if not (signed_tx or "").strip():
        return _reject(["MISSING_SIGNATURE"], "no wallet signature was provided")
    if (
        not binding.strategy_id.strip()
        or not binding.owner_account_id.strip()
        or binding.source != "stash"
    ):
        return _reject(
            ["FLOW_UNBOUND"],
            "that flow is not bound to the stock sleeve; nothing moved",
        )

    from miriam_agent.tools.glider_sleeve import SOLANA_CHAIN_ID, parse_caip10

    try:
        parsed_binding_owner = parse_caip10(binding.owner_account_id)
        if parsed_binding_owner.get("namespace") != "solana":
            raise ValueError("bound owner is not a Solana account")
        amount = money(binding.amount)
    except Exception as exc:  # noqa: BLE001 - a corrupt binding is a refusal
        return _reject(
            ["FLOW_UNBOUND"], f"that flow carries an invalid binding ({exc})"
        )
    if amount > ledger.balance(INVEST_SLEEVE):
        # Checked before the provider call so a refusal never moves money.
        binding.status = "failed"
        ledger.pending_invest[flow_id] = binding
        await store.save(ledger)
        return _reject(
            ["OVER_BALANCE"],
            f"the stash holds {ledger.balance(INVEST_SLEEVE)}, "
            f"which is less than {amount}; nothing moved",
        )
    try:
        enrolled = await _obtain_and_replay(
            complete_call,
            {
                "strategy_id": binding.strategy_id,
                "owner_account_id": binding.owner_account_id,
                "flow_id": binding.flow_id,
                "account_index": binding.account_index,
                "agent_account_id": binding.agent_account_id,
                "chain_ids": [SOLANA_CHAIN_ID],
                "signed_solana_transaction": signed_tx.strip(),
                "amount_usd": float(amount),
                "source": "stash",
            },
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure is a business result
        logger.warning("invest settle: stage 2 failed: %s", exc)
        binding.status = "failed"
        ledger.pending_invest[flow_id] = binding
        await store.save(ledger)
        return _reject(
            ["SETTLE_FAILED"], f"Glider did not enroll ({exc}); nothing moved"
        )

    if str(enrolled.get("status") or "") == "AWAITING_CONFIRMATION":
        return _reject(
            ["STILL_STAGED"], "the provider stayed staged after replay; nothing moved"
        )

    enrollment, funding = _extract_funding(enrolled)
    if str(funding.get("status") or "").upper() == "FAILED":
        binding.status = "failed"
        ledger.pending_invest[flow_id] = binding
        await store.save(ledger)
        reason = (
            funding.get("failure_reason") or funding.get("FailureReason") or "unknown"
        )
        return _reject(
            ["FUNDING_FAILED"],
            f"enrolled but funding failed ({reason}); sleeve not debited",
        )

    try:
        ledger.debit(INVEST_SLEEVE, amount)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - remote moved, local refused: diverge loudly
        logger.error(
            "invest settle diverged: Go funded flow %s but stash debit refused (%s)",
            flow_id,
            exc,
        )
        binding.status = "failed"
        ledger.pending_invest[flow_id] = binding
        receipt = Receipt(
            id=_id("rcpt"),
            at=timestamp,
            status="parked",
            action="invest_settle",
            currency=ledger.currency,
            amount=amount,
            counterparty="Rail Stock Sleeve",
            sleeve=INVEST_SLEEVE,
            decision_id=decision_id,
            idempotency_key=f"invest-settle:{flow_id}",
            reasons=["LEDGER_DIVERGED"],
            sleeves_before=before,
            sleeves_after=sleeves_snapshot(ledger.sleeves),
            rail_reference=str(
                enrollment.get("glider_portfolio_id")
                or enrollment.get("gliderPortfolioId")
                or ""
            ),
            detail=(
                f"Go funded ${amount} but the local stash debit refused ({exc}); "
                "money moved remotely and needs reconciliation; not re-tappable "
                "under this key"
            ),
        )
        ledger.remember_receipt(receipt)
        await store.save(ledger)
        return (
            receipt,
            {
                "ok": False,
                "reasons": ["LEDGER_DIVERGED"],
                "detail": receipt.detail,
            },
            ledger,
        )

    binding.status = "enrolled"
    ledger.pending_invest[flow_id] = binding
    ledger.record_movement(
        Movement(
            kind="outflow",
            amount=amount,
            sleeve=INVEST_SLEEVE,
            counterparty="Rail Stock Sleeve",
            category="invest",
            ref=flow_id,
            at=timestamp,
        )
    )
    receipt = Receipt(
        id=_id("rcpt"),
        at=timestamp,
        status="executed",
        action="invest_settle",
        currency=ledger.currency,
        amount=amount,
        counterparty="Rail Stock Sleeve",
        sleeve=INVEST_SLEEVE,
        decision_id=decision_id,
        idempotency_key=f"invest-settle:{flow_id}",
        sleeves_before=before,
        sleeves_after=sleeves_snapshot(ledger.sleeves),
        rail_reference=str(
            (
                enrollment.get("glider_portfolio_id")
                or enrollment.get("gliderPortfolioId")
                or ""
            )
            if isinstance(enrollment, dict)
            else ""
        ),
        detail=f"${amount} in Rail Stock Sleeve",
    )
    ledger.remember_receipt(receipt)
    await store.save(ledger)
    positions = enrolled.get("positions") if isinstance(enrolled, dict) else None
    result: dict[str, Any] = {
        "ok": True,
        "flow_id": flow_id,
        "enrollment": enrollment,
        "funding": funding,
        "positions": positions if isinstance(positions, list) else [],
    }
    return receipt, result, ledger


__all__ = [
    "ALLOCATE_TTL_MINUTES",
    "INVEST_SLEEVE",
    "INVEST_SOURCE",
    "invest_amount_ok",
    "parse_invest_utterance",
    "prepare_allocate",
    "settle_allocate",
]
