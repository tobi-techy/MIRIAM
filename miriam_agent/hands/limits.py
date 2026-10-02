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
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from miriam_agent.hands.ledger import SLEEVES, money

if TYPE_CHECKING:
    from miriam_agent.hands.ledger import Ledger

# Fail-closed fallback when MAX_DAILY_TRANSFER is missing from settings. A daily
# cap that silently becomes unlimited is worse than one that is too tight.
DAILY_CAP_DEFAULT = Decimal("10000")
WEEKLY_CAP_DEFAULT = Decimal("500000")
VELOCITY_DEFAULT = 20


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
    max_weekly: Decimal = Decimal("500000")
    max_per_hour: int = 20
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
        weekly = getattr(settings, "MAX_WEEKLY_TRANSFER", None)
        per_hour = getattr(settings, "MAX_TRANSFERS_PER_HOUR", None)
        return cls(
            max_auto=money(settings.APPROVAL_REQUIRED_ABOVE),
            max_with_confirm=money(settings.MAX_TRANSACTION_AMOUNT),
            reversible_under=money(settings.APPROVAL_REQUIRED_ABOVE),
            max_daily=(money(daily) if daily is not None else money(DAILY_CAP_DEFAULT)),
            max_weekly=(
                money(weekly) if weekly is not None else money(WEEKLY_CAP_DEFAULT)
            ),
            max_per_hour=(int(per_hour) if per_hour is not None else VELOCITY_DEFAULT),
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
            "max_weekly": str(self.max_weekly),
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


def settled_outbound_since(ledger: Ledger, *, since: datetime) -> Decimal:
    """Settled outbound P2P-like sends at or after ``since``.

    Only executed ``transfer`` receipts count: inflow splits, internal moves,
    yield routes, declines and rejections are never counted.
    """
    total = Decimal("0")
    for receipt in ledger.receipts:
        receipt_at = receipt.at
        if receipt_at.tzinfo is None:
            continue
        if receipt_at.astimezone(since.tzinfo) < since:
            continue
        if receipt.status != "executed":
            continue
        if receipt.action != "transfer":
            continue
        if receipt.amount is None:
            continue
        total += money(receipt.amount)
    return money(total)


def settled_outbound_today(ledger: Ledger, *, at: datetime | None = None) -> Decimal:
    """Settled outbound P2P-like sends for this user since local-day start.

    Measured from receipt records (executed transfer receipts), not from
    movements, so it covers exactly what Hands debited through execute_transfer.
    Inflow splits, internal moves, yield routes, declines and rejections are
    never counted.
    """
    return settled_outbound_since(ledger, since=_day_start(at))


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


def _week_start(at: datetime | None) -> datetime:
    """Monday that starts the trailing week, in the money timezone."""
    day_start = _day_start(at)
    return day_start - timedelta(days=day_start.weekday())


def weekly_usage(ledger: Ledger, *, at: datetime | None = None) -> Decimal:
    """Settled (since Monday) plus reserved outbound, the weekly cap figure."""
    return money(
        settled_outbound_since(ledger, since=_week_start(at))
        + reserved_outbound(ledger, at=at)
    )


def mutations_in_window(
    ledger: Ledger, *, window_minutes: int, at: datetime | None = None
) -> int:
    """Number of settled outbound transfers in the trailing ``window_minutes``.

    Velocity is measured on executed ``transfer`` receipts only -- the P2P-like
    sends that are the highest-friction, highest-risk surface.
    """
    now = at or datetime.now().astimezone()
    since = now - timedelta(minutes=window_minutes)
    count = 0
    for receipt in ledger.receipts:
        if receipt.status != "executed":
            continue
        if receipt.action != "transfer":
            continue
        receipt_at = receipt.at
        if receipt_at.tzinfo is None:
            continue
        if receipt_at.astimezone(now.tzinfo) >= since:
            count += 1
    return count


def _day_start(at: datetime | None) -> datetime:
    """Midnight that starts the user's financial day, in the money timezone.

    The boundary used to be the server's local timezone, so the day a user's
    cap reset on depended on wherever the process happened to run. It is now
    pinned by ``MONEY_DAY_TIMEZONE`` (default: Africa/Lagos, the product's
    home market) with a UTC fallback for an unset or unknown zone.
    """
    from datetime import UTC
    from zoneinfo import ZoneInfo

    moment = at or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        from miriam_agent.config.settings import get_settings

        zone = ZoneInfo(get_settings().MONEY_DAY_TIMEZONE)
    except Exception:
        zone = UTC
    return moment.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)


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
        # The cap is inclusive: a send that brings the day's total to exactly
        # max_daily is allowed; only exceeding it is a breach. The old `>=`
        # refused the final slot of the allowance forever.
        if money(used) + money(amount) > policy.max_daily:
            reasons.append("DAILY_CAP")
        if money(weekly_usage(ledger, at=at)) + money(amount) > policy.max_weekly:
            reasons.append("WEEKLY_CAP")
        if mutations_in_window(ledger, window_minutes=60, at=at) >= policy.max_per_hour:
            reasons.append("VELOCITY")
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
            "WEEKLY_CAP",
            "VELOCITY",
        }
    ]
    return LimitReport(allowed=not known, reasons=reasons, cap=cap)


class LimitVerdict(StrEnum):
    """The three-tier autonomy outcome for one proposed movement."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True)
class LimitDecision:
    """A verdict plus the report that produced it."""

    verdict: LimitVerdict
    report: LimitReport
    confirm_needed: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict is LimitVerdict.ALLOW


def classify_limits(
    *,
    policy: Policy,
    amount: Decimal,
    sleeve: str,
    spendable: Decimal,
    rent_required: Decimal,
    rent_reserved: Decimal,
    ledger: Ledger | None = None,
    at: datetime | None = None,
) -> LimitDecision:
    """Map a proposed movement to ALLOW / CONFIRM / BLOCK.

    * any limit breach (non-positive, over tx cap, locked, over balance,
      daily/weekly cap, velocity) -> BLOCK
    * within limits but above the act-without-asking ceiling -> CONFIRM
    * otherwise -> ALLOW

    ``evaluate_limits`` stays the single source of truth for what is allowed;
    this only layers the autonomy tier on top of it.
    """
    report = evaluate_limits(
        policy=policy,
        amount=amount,
        sleeve=sleeve,
        spendable=spendable,
        rent_required=rent_required,
        rent_reserved=rent_reserved,
        ledger=ledger,
        at=at,
    )
    if not report.allowed:
        return LimitDecision(verdict=LimitVerdict.BLOCK, report=report)
    if needs_confirm(policy, amount):
        return LimitDecision(
            verdict=LimitVerdict.CONFIRM, report=report, confirm_needed=True
        )
    return LimitDecision(verdict=LimitVerdict.ALLOW, report=report)


def known_sleeves() -> tuple[str, ...]:
    """The sleeve vocabulary, so a typo cannot invent a fifth balance."""
    return SLEEVES


__all__ = [
    "DAILY_CAP_DEFAULT",
    "VELOCITY_DEFAULT",
    "WEEKLY_CAP_DEFAULT",
    "LimitDecision",
    "LimitReport",
    "LimitVerdict",
    "Policy",
    "affordable_cap",
    "check_amount",
    "classify_limits",
    "daily_usage",
    "evaluate_limits",
    "free_after_obligations",
    "known_sleeves",
    "mutations_in_window",
    "needs_confirm",
    "reserved_outbound",
    "settled_outbound_since",
    "settled_outbound_today",
    "weekly_usage",
]
