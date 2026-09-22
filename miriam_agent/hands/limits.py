"""Layer 1 - HANDS. Policy limits and the affordability cap.

Deterministic code only. No LLM.

Two jobs live here, and they are deliberately different:

* :func:`check_amount` answers "does this break a limit?", which is the
  question Judgment asks and the question Hands enforces a second time.
* :func:`affordable_cap` answers "what is the largest version of this that
  would still be safe?", which is what ``allow_smaller`` needs. Judgment only
  picks the label; the number is computed here, in code, from the ledger.

Sleeves can be locked by policy. A locked sleeve is not a suggestion: nothing
in this package will move money out of it, and there is no flag a caller can
set to make it happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from miriam_agent.hands.ledger import SLEEVES, money

if TYPE_CHECKING:
    from miriam_agent.hands.ledger import Ledger

# Fail-closed fallback when MAX_DAILY_TRANSFER is missing from settings. A daily
# cap that silently becomes unlimited is worse than one that is too tight.
DAILY_CAP_DEFAULT = Decimal("10000")


@dataclass(frozen=True)
class Policy:
    """The limits a movement is measured against.

    ``max_auto`` is the ceiling for acting without asking. ``max_with_confirm``
    is the ceiling for acting at all, even with one. ``reversible_under`` is the
    band in which an action may be taken back. ``max_daily`` caps settled plus
    reserved outbound P2P-like sends per user per local day.
    """

    max_auto: Decimal = Decimal("2000")
    max_with_confirm: Decimal = Decimal("100000")
    reversible_under: Decimal = Decimal("5000")
    max_daily: Decimal = Decimal("10000")
    locked_sleeves: tuple[str, ...] = ("locked",)
    # Where a yield route parks when the yield rail is down. Never a sleeve the
    # user cannot reach, and never a claim that the yield posted.
    yield_fallback: str = "savings"

    @classmethod
    def from_settings(cls) -> Policy:
        """Build the policy from the single source of truth (config/settings).

        A missing MAX_DAILY_TRANSFER fails closed to DAILY_CAP_DEFAULT rather
        than to unlimited: an absent cap must refuse sooner, not move more.
        """
        from miriam_agent.config.settings import get_settings

        settings = get_settings()
        daily = getattr(settings, "MAX_DAILY_TRANSFER", None)
        return cls(
            max_auto=money(settings.APPROVAL_REQUIRED_ABOVE),
            max_with_confirm=money(settings.MAX_TRANSACTION_AMOUNT),
            reversible_under=money(settings.APPROVAL_REQUIRED_ABOVE),
            max_daily=(
                money(daily) if daily is not None else money(DAILY_CAP_DEFAULT)
            ),
        )

    def is_locked(self, sleeve: str) -> bool:
        return sleeve in self.locked_sleeves

    def to_state(self) -> dict[str, object]:
        """The policy slice that ships inside STATE."""
        return {
            "max_auto": str(self.max_auto),
            "max_with_confirm": str(self.max_with_confirm),
            "reversible_under": str(self.reversible_under),
            "max_daily": str(self.max_daily),
            "locked_sleeves": list(self.locked_sleeves),
        }


def check_amount(policy: Policy, amount: Decimal) -> list[str]:
    """Reason codes for a limit breach. Empty means within every limit."""
    reasons: list[str] = []
    if amount <= 0:
        reasons.append("NON_POSITIVE_AMOUNT")
    if amount > policy.max_with_confirm:
        reasons.append("OVER_LIMIT")
    return reasons


def needs_confirm(policy: Policy, amount: Decimal) -> bool:
    """Whether this amount is above the act-without-asking ceiling."""
    return amount > policy.max_auto


def free_after_obligations(
    state_spendable: Decimal, rent_required: Decimal, rent_reserved: Decimal
) -> Decimal:
    """Spendable money that is genuinely the user's to move.

    Rent that is required but not yet reserved is still owed, so it is not
    available even though it is technically sitting in the spendable sleeve.
    Money already reserved has been moved out of spendable, which is why only
    the unreserved remainder is subtracted here.
    """
    owed = max(Decimal("0"), rent_required - rent_reserved)
    return state_spendable - owed


def affordable_cap(
    *,
    policy: Policy,
    spendable: Decimal,
    rent_required: Decimal,
    rent_reserved: Decimal,
) -> Decimal:
    """The largest safe movement right now, or zero when none is safe.

    Hands computes this; Judgment only decides whether to use it. The cap never
    exceeds the spendable sleeve after rent is protected, never exceeds the
    act-without-asking ceiling, and never goes negative.
    """
    free = free_after_obligations(spendable, rent_required, rent_reserved)
    cap = min(free, policy.max_auto)
    return money(cap) if cap > 0 else Decimal("0")


@dataclass
class LimitReport:
    """The outcome of a limit check, with the numbers behind it."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)
    cap: Decimal = Decimal("0")


def settled_outbound_today(ledger: Ledger, *, at: datetime | None = None) -> Decimal:
    """Settled outbound P2P-like sends for this user since local-day start.

    Measured from receipt records (executed transfer receipts), not from
    movements, so it covers exactly what Hands debited through execute_transfer.
    Inflow splits, internal moves, yield routes, declines and rejections are
    never counted.
    """
    day_start = _day_start(at)
    total = Decimal("0")
    for receipt in ledger.receipts:
        receipt_at = receipt.at
        if receipt_at.tzinfo is None:
            continue
        if receipt_at.astimezone(day_start.tzinfo) < day_start:
            continue
        if receipt.status != "executed":
            continue
        if receipt.action != "transfer":
            continue
        if receipt.amount is None:
            continue
        total += money(receipt.amount)
    return money(total)


def reserved_outbound(ledger: Ledger, *, at: datetime | None = None) -> Decimal:
    """Pending confirm amounts still reserved against the daily cap.

    A confirm created but not yet settled, expired, consumed or declined holds
    its amount off the cap so a second send cannot race it. Declined and
    consumed challenges release their hold; open ones keep it.
    """
    now = at or datetime.now().astimezone()
    total = Decimal("0")
    for challenge in ledger.challenges.values():
        if not challenge.is_open(now):
            continue
        total += money(challenge.amount)
    return money(total)


def daily_usage(ledger: Ledger, *, at: datetime | None = None) -> Decimal:
    """Settled plus reserved outbound, the number the cap is measured against."""
    return money(
        settled_outbound_today(ledger, at=at) + reserved_outbound(ledger, at=at)
    )


def _day_start(at: datetime | None) -> datetime:
    moment = at or datetime.now().astimezone()
    if moment.tzinfo is None:
        from datetime import UTC

        moment = moment.replace(tzinfo=UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def evaluate_limits(
    *,
    policy: Policy,
    amount: Decimal,
    sleeve: str,
    spendable: Decimal,
    rent_required: Decimal,
    rent_reserved: Decimal,
    ledger: Ledger | None = None,
    at: datetime | None = None,
) -> LimitReport:
    """Run every limit check for one proposed movement.

    The daily cap covers settled plus still-open pending confirms. A breach
    rejects with DAILY_CAP and names the numbers in the cap field, which callers
    record on a rejected receipt with sleeves unchanged.
    """
    reasons = check_amount(policy, amount)
    if policy.is_locked(sleeve):
        reasons.append("LOCKED_SLEEVE")
    free = free_after_obligations(spendable, rent_required, rent_reserved)
    if amount > free:
        reasons.append("OVER_BALANCE")
    cap = affordable_cap(
        policy=policy,
        spendable=spendable,
        rent_required=rent_required,
        rent_reserved=rent_reserved,
    )
    used = Decimal("0")
    if ledger is not None:
        used = daily_usage(ledger, at=at)
        if money(used) + money(amount) >= policy.max_daily:
            reasons.append("DAILY_CAP")
    known = [
        r
        for r in reasons
        if r
        in {
            "NON_POSITIVE_AMOUNT",
            "OVER_LIMIT",
            "LOCKED_SLEEVE",
            "OVER_BALANCE",
            "DAILY_CAP",
        }
    ]
    return LimitReport(allowed=not known, reasons=reasons, cap=cap)


def known_sleeves() -> tuple[str, ...]:
    """The sleeve vocabulary, so a typo cannot invent a fifth balance."""
    return SLEEVES


__all__ = [
    "DAILY_CAP_DEFAULT",
    "LimitReport",
    "Policy",
    "affordable_cap",
    "check_amount",
    "daily_usage",
    "evaluate_limits",
    "free_after_obligations",
    "known_sleeves",
    "needs_confirm",
    "reserved_outbound",
    "settled_outbound_today",
]
