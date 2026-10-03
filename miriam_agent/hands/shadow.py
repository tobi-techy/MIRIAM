"""Layer 1 - HANDS. Shadow-run parity: predict a turn's money outcome dry.

The trust gate's second half. The regression suites prove the rails in
isolation; the shadow proves the *wired* path agrees with production on real
turns. Given a recorded turn (the action the live path parsed, the ledger
snapshot it saw, the policy, and the receipt it produced), the shadow re-runs
the trust boundary -- asset resolution, limit classification, journal claim --
against isolated fakes and compares the prediction with what actually happened.

The shadow NEVER touches a rail, NEVER writes a ledger, and NEVER mints a
claim in a shared journal: its journal is private per run, its rail is absent
by construction (the prediction stops at "would dispatch"). Any divergence
between predicted and actual is a fact for an operator, never an auto-fix:
like the reconciler, this module reports, it does not heal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

logger = logging.getLogger(__name__)

Prediction = Literal["would_execute", "would_confirm", "would_refuse"]


@dataclass(frozen=True)
class RecordedTurn:
    """One production turn, frozen for replay."""

    turn_id: str
    action_type: str
    amount: str
    currency: str
    counterparty: str
    sleeve: str
    spendable: str
    rent_required: str = "0"
    rent_reserved: str = "0"
    # What the live path produced: executed | rejected (+ reasons) | challenged.
    actual_status: str = ""
    actual_reasons: tuple[str, ...] = ()
    # For order/invest turns: the raw asset words the user used.
    asset_query: str = ""


@dataclass
class ShadowVerdict:
    """The shadow's prediction vs the live outcome."""

    turn_id: str
    predicted: Prediction
    predicted_reasons: tuple[str, ...] = ()
    actual_status: str = ""
    diverged: bool = False
    divergence_note: str = ""
    asset_resolution: str = ""


def _to_decimal(value: str) -> Decimal:
    from miriam_agent.hands.ledger import money

    return money(value)


async def predict_turn(
    turn: RecordedTurn,
    *,
    policy=None,
    at: datetime | None = None,
) -> ShadowVerdict:
    """Run the trust boundary dry over a recorded turn.

    Read-only by construction: the journal is private to this call, no rail
    object exists anywhere in this module, and the ledger is never loaded.
    """
    from miriam_agent.hands.assets import resolve_asset
    from miriam_agent.hands.execution_journal import InMemoryExecutionJournal
    from miriam_agent.hands.limits import LimitVerdict, Policy, classify_limits

    policy = policy if policy is not None else Policy()
    moment = at if at is not None else datetime.now(UTC)

    asset_note = ""
    if turn.action_type in ("order", "invest") and turn.asset_query:
        resolution = resolve_asset(turn.asset_query)
        asset_note = resolution.kind
        if resolution.kind == "unknown":
            return ShadowVerdict(
                turn_id=turn.turn_id,
                predicted="would_refuse",
                predicted_reasons=("UNRESOLVED_ASSET",),
                actual_status=turn.actual_status,
                asset_resolution="unknown",
            )
        if resolution.kind == "ambiguous":
            return ShadowVerdict(
                turn_id=turn.turn_id,
                predicted="would_confirm",
                predicted_reasons=("AMBIGUOUS_ASSET",),
                actual_status=turn.actual_status,
                asset_resolution="ambiguous",
            )
        asset_note = f"resolved:{resolution.asset.symbol}"

    # Challenge-only actions mirror the orchestrator: invest, orders,
    # rebalances, funding legs and bills never execute from an utterance --
    # the tap authorises the money -- so within limits they predict
    # ``would_confirm``, never ``would_execute``. A limit breach still refuses.
    _CHALLENGE_ONLY = frozenset(
        {
            "invest",
            "order",
            "rebalance",
            "onramp",
            "offramp",
            "bill",
            "set_allocation",
            "pause",
            "resume",
            "save_rule",
        }
    )
    challenge_only = turn.action_type in _CHALLENGE_ONLY

    decision = classify_limits(
        policy=policy,
        amount=_to_decimal(turn.amount),
        sleeve=turn.sleeve,
        spendable=_to_decimal(turn.spendable),
        rent_required=_to_decimal(turn.rent_required),
        rent_reserved=_to_decimal(turn.rent_reserved),
        ledger=None,
        at=moment,
    )
    if decision.verdict is LimitVerdict.BLOCK:
        predicted: Prediction = "would_refuse"
    elif decision.verdict is LimitVerdict.CONFIRM or challenge_only:
        predicted = "would_confirm"
    else:
        predicted = "would_execute"

    # The journal claim, on a private journal: proves the key *format* claims
    # cleanly and the claim path is reachable. A lost claim here means the
    # turn's key material is malformed, which is itself a divergence signal.
    journal = InMemoryExecutionJournal()
    try:
        claim = await journal.reserve(
            key=f"shadow:{turn.turn_id}",
            user_id="shadow",
            action=turn.action_type,
            amount=_to_decimal(turn.amount),
            currency=turn.currency,
            counterparty=turn.counterparty,
            sleeve=turn.sleeve,
            decision_id=f"shadow:{turn.turn_id}",
        )
        claim_ok = claim.won
    except Exception as exc:  # noqa: BLE001 - claim failure is a signal, not a crash
        logger.warning("shadow claim failed for %s: %s", turn.turn_id, exc)
        claim_ok = False

    reasons = tuple(decision.report.reasons)
    if not claim_ok:
        reasons = reasons + ("SHADOW_CLAIM_FAILED",)
        predicted = "would_refuse"

    return ShadowVerdict(
        turn_id=turn.turn_id,
        predicted=predicted,
        predicted_reasons=reasons,
        actual_status=turn.actual_status,
        asset_resolution=asset_note,
    )


def compare(verdict: ShadowVerdict) -> ShadowVerdict:
    """Mark divergence between the shadow prediction and the live outcome.

    Mapping: live ``executed`` must predict ``would_execute``; live
    ``challenged`` (confirm card staged) must predict ``would_confirm``;
    live ``rejected`` must predict ``would_refuse``. Anything else is a
    divergence with a note, for an operator to triage.
    """
    expected = {
        "executed": "would_execute",
        "challenged": "would_confirm",
        "rejected": "would_refuse",
    }.get(verdict.actual_status)
    if expected is None:
        # Unknown live status (e.g. a future outcome kind): not a divergence
        # the shadow can judge, so it passes through unmarked.
        return verdict
    diverged = verdict.predicted != expected
    note = (
        ""
        if not diverged
        else (
            f"shadow predicted {verdict.predicted} "
            f"({','.join(verdict.predicted_reasons) or 'no reasons'}) "
            f"but live was {verdict.actual_status}"
        )
    )
    return ShadowVerdict(
        turn_id=verdict.turn_id,
        predicted=verdict.predicted,
        predicted_reasons=verdict.predicted_reasons,
        actual_status=verdict.actual_status,
        diverged=diverged,
        divergence_note=note,
        asset_resolution=verdict.asset_resolution,
    )


@dataclass
class ShadowReport:
    """The outcome of one parity run over a corpus."""

    at: datetime
    total: int = 0
    diverged: list[ShadowVerdict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.diverged


async def run_corpus(
    turns: list[RecordedTurn],
    *,
    policy=None,
    at: datetime | None = None,
) -> ShadowReport:
    """Predict + compare every recorded turn. Read-only throughout."""
    report = ShadowReport(at=at if at is not None else datetime.now(UTC))
    for turn in turns:
        verdict = compare(await predict_turn(turn, policy=policy, at=at))
        report.total += 1
        if verdict.diverged:
            report.diverged.append(verdict)
    return report


def turn_from_dict(raw: dict[str, Any]) -> RecordedTurn:
    """Parse one JSONL corpus line. Unknown fields are ignored, never trusted."""
    reasons = raw.get("actual_reasons") or []
    return RecordedTurn(
        turn_id=str(raw.get("turn_id", "")),
        action_type=str(raw.get("action_type", "")),
        amount=str(raw.get("amount", "0")),
        currency=str(raw.get("currency", "NGN")),
        counterparty=str(raw.get("counterparty", "")),
        sleeve=str(raw.get("sleeve", "spendable")),
        spendable=str(raw.get("spendable", "0")),
        rent_required=str(raw.get("rent_required", "0")),
        rent_reserved=str(raw.get("rent_reserved", "0")),
        actual_status=str(raw.get("actual_status", "")),
        actual_reasons=tuple(str(r) for r in reasons),
        asset_query=str(raw.get("asset_query", "")),
    )


__all__ = [
    "RecordedTurn",
    "ShadowReport",
    "ShadowVerdict",
    "compare",
    "predict_turn",
    "run_corpus",
    "turn_from_dict",
]
