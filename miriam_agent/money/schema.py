"""The MoneyPlan output contract.

Every user-facing money answer is an instance of :class:`MoneyPlan`, then
rendered. The LLM never constructs one -- Python does (``MONEY-PLAN-CONTRACT.md``).

Two things here are structural rather than advisory:

  - ``BufferPlan.is_glider`` is ``Literal[False]``. Emergency money cannot be
    represented as being in a Glider strategy, because there is no value that
    would say so.
  - :meth:`MoneyPlan._guard_gated_investing` refuses to construct a plan that has
    investable surplus, or a Glider action, while the book is gated. The safety
    ordering (``MONEY-RULES.md`` §3) is therefore enforced by the type system,
    not by remembering to check a flag.

Money is ``Decimal`` throughout and converted only at the boundary.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from miriam_agent.money.formatting import weight_str as _weight_str

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Ordered by urgency; ``diagnose.py`` walks this order and the first match wins.
PROBLEM_TYPES: tuple[str, ...] = (
    "cashflow_bleed",
    "no_buffer",
    "high_interest_debt",
    "inflation_erosion",
    "goal_mismatch",
    "under_earning",
    "idle_surplus",
    "overconfidence_risk",
    "data_gap",
)

ProblemType = Literal[
    "cashflow_bleed",
    "no_buffer",
    "high_interest_debt",
    "inflation_erosion",
    "goal_mismatch",
    "under_earning",
    "idle_surplus",
    "overconfidence_risk",
    "data_gap",
]

Confidence = Literal["high", "medium", "low"]

# Debt triage bands (``MONEY-RULES.md`` §3).
DebtBand = Literal["fire", "judgment", "keep", "none"]

# How the extra payment is aimed (``R-RAMSEY-3``).
DebtStrategy = Literal["snowball", "avalanche", "minimum_only"]

GliderActionKind = Literal["none", "draft", "monitor"]

# Where a draft stands. ``draft_local`` is the honest default: built and
# inspectable, never submitted to Glider.
GliderDraftStatus = Literal["draft_local", "validated", "rejected"]


DISCLAIMER = (
    "Miriam is not a licensed financial adviser. This is a plan computed from "
    "the numbers you provided, not personalised regulated advice. Onchain "
    "investing carries smart contract, liquidity, depeg, chain, and key-"
    "management risk, has no deposit insurance, and you can lose principal."
)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class CashflowSplit(BaseModel):
    """Take-home split into fixed / debt / savings / investments / guilt-free.

    Debt minimums live in ``fixed`` (they are unavoidable). ``debt`` is the
    extra attack above the minimum, which is why it gets its own line: money
    that attacks a 35% APR balance is not "savings" and must not be shown as
    such.
    """

    model_config = ConfigDict(extra="forbid")

    fixed: Decimal = Decimal("0")
    debt: Decimal = Decimal("0")
    savings: Decimal = Decimal("0")
    investments: Decimal = Decimal("0")
    guilt_free: Decimal = Decimal("0")
    currency: str = "NGN"
    shares: dict[str, float] = Field(default_factory=dict)
    crisis: bool = False
    note: str = ""

    def total(self) -> Decimal:
        return (
            self.fixed + self.debt + self.savings + self.investments + self.guilt_free
        )


class BufferPlan(BaseModel):
    """The emergency buffer target and the gap to it.

    ``is_glider`` is pinned to ``False``: emergency money is never in a Glider
    strategy, and this model cannot express otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    target_months: Decimal = Decimal("0")
    target_amount: Decimal = Decimal("0")
    current_amount: Decimal = Decimal("0")
    gap: Decimal = Decimal("0")
    months_to_fill: Decimal | None = None
    vehicle: str = ""
    location: str = ""
    is_glider: Literal[False] = False


class DebtAction(BaseModel):
    """One debt and what to do about it."""

    model_config = ConfigDict(extra="forbid")

    label: str
    balance: Decimal
    apr_pct: Decimal
    minimum_monthly: Decimal = Decimal("0")
    extra_monthly: Decimal = Decimal("0")
    band: DebtBand = "none"
    strategy: DebtStrategy = "minimum_only"
    reason: str = ""


class AllocationBook(BaseModel):
    """The growth/defensive book, or the absence of one.

    Two different reasons a book can have no growth sleeve, and they are not the
    same thing:

      - ``gated``: the *safety stack* refused. There is no investable surplus and
        the money is not the user's to risk.
      - ``short_horizon``: the safety stack allowed investing, but the goal is
        inside three years, so the money is held cash-like instead. The surplus
        exists; it just belongs in savings rather than a market sleeve.

    ``investable_surplus`` is the authoritative figure the plan uses -- this
    engine owns it, because only here is the horizon known.
    """

    model_config = ConfigDict(extra="forbid")

    gated: bool = False
    short_horizon: bool = False
    growth_pct: Decimal = Decimal("0")
    defensive_pct: Decimal = Decimal("0")
    investable_surplus: Decimal = Decimal("0")
    rule_id: str = ""
    overrides: list[str] = Field(default_factory=list)
    reason: str = ""
    growth_sleeve: list[str] = Field(default_factory=list)
    defensive_sleeve: list[str] = Field(default_factory=list)

    @property
    def is_gated(self) -> bool:
        return self.gated


class DraftWeight(BaseModel):
    """One line of a strategy draft.

    ``asset_class`` is always present, because that is what the book is actually
    made of. ``asset_id`` is filled only when a real CAIP-19 id has been resolved
    from a real source; an empty id is correct in offline mode and must never be
    guessed to make the draft look finished.
    """

    model_config = ConfigDict(extra="forbid")

    asset_class: str
    weight: Decimal
    asset_id: str = ""


class GliderDraft(BaseModel):
    """A complete, inspectable strategy the user could sign.

    Weights are percent strings that sum to exactly 100, matching what
    ``POST /v2/strategies`` accepts, so the draft is a real proposal rather than
    a description of one.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    template: str
    book: str
    weights: list[DraftWeight] = Field(default_factory=list)
    schedule: dict[str, str] = Field(default_factory=dict)
    status: GliderDraftStatus = "draft_local"
    submitted: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _weights_sum_to_100(self) -> GliderDraft:
        if not self.weights:
            raise ValueError("a Glider draft needs at least one weight")
        total = sum((w.weight for w in self.weights), Decimal("0"))
        if total != Decimal("100"):
            raise ValueError(f"draft weights must sum to 100, got {total}")
        return self

    def as_payload(self) -> dict:
        """The request body for ``POST /v2/strategies``.

        Refuses when any weight has no resolved asset id, because a payload with
        a blank id is not a draft we can submit, it is a draft we would be
        guessing on.
        """
        missing = [w.asset_class for w in self.weights if not w.asset_id]
        if missing:
            raise ValueError(
                "cannot build a submit-ready payload without resolved asset ids "
                "for: " + ", ".join(missing)
            )
        return {
            "name": self.name,
            "allocation": {
                "assets": [
                    {"assetId": w.asset_id, "weight": _weight_str(w.weight)}
                    for w in self.weights
                ]
            },
            "schedule": dict(self.schedule)
            or {"type": "interval", "frequency": "monthly"},
        }


class GliderAction(BaseModel):
    """What to do about Glider: nothing, draft a strategy, or watch one."""

    model_config = ConfigDict(extra="forbid")

    kind: GliderActionKind = "none"
    template: str = ""
    strategy_id: str = ""
    portfolio_id: str = ""
    draft: GliderDraft | None = None
    validation: str = ""
    drift: dict[str, float] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    blocked_reason: str = ""

    @model_validator(mode="after")
    def _require_target(self) -> GliderAction:
        """Each kind must carry the thing that makes it meaningful.

        A refusal must say why, a draft must be a complete proposal, and a
        monitor must name what it is watching. There is no "recommended a thing
        you cannot inspect" state.
        """
        if self.kind == "none" and not self.blocked_reason:
            raise ValueError("a Glider refusal must give a reason")
        if self.kind == "draft":
            if self.draft is None:
                raise ValueError("a Glider draft must include the draft itself")
            if self.draft.submitted:
                raise ValueError(
                    "a draft is never submitted; enrollment is user-signed"
                )
        if self.kind == "monitor" and not self.portfolio_id:
            raise ValueError("monitoring a portfolio requires a portfolio_id")
        return self


class Action(BaseModel):
    """One concrete 90-day action."""

    model_config = ConfigDict(extra="forbid")

    when: str  # "this week" | "this payday" | "this month"
    what: str
    amount: Decimal | None = None
    currency: str = ""
    how: str = ""


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

# The disclosures that ride along with every plan (``R-AUDIT-3``).
GLIDER_RISKS: tuple[str, ...] = (
    "Smart contract risk: a bug or exploit in the contracts or the smart account "
    "can lose funds.",
    "Stablecoin depeg risk: a stablecoin can trade away from its peg, and the "
    "loss is real.",
    "Liquidity risk: exiting a position may mean slippage, or may not be possible "
    "at the price you saw.",
    "Chain risk: the network itself can congest, halt, or change.",
    "Key-management risk: whoever holds the keys holds the money, and a mistake "
    "is usually unrecoverable.",
    "No deposit insurance. Unlike an insured bank balance, there is no scheme that "
    "makes you whole.",
    "Behaviour risk: the plan only works if you leave it alone through a "
    "drawdown. Selling at the bottom is the failure mode.",
)


class MoneyPlan(BaseModel):
    """The structured plan Miriam renders. Computed, never generated."""

    model_config = ConfigDict(extra="forbid")

    diagnosis: str = Field(min_length=1)
    problem_type: ProblemType
    currency: str

    monthly_take_home: Decimal = Decimal("0")
    cashflow: CashflowSplit
    buffer: BufferPlan
    debts: list[DebtAction] = Field(default_factory=list)
    # Investable surplus: money that cleared the safety stack AND has a 5+ year
    # horizon. Zero whenever investing is gated.
    surplus_monthly: Decimal = Decimal("0")
    book: AllocationBook
    glider: GliderAction

    actions_90d: list[Action] = Field(default_factory=list)
    automation_rules: list[str] = Field(default_factory=list)
    kill_switches: list[str] = Field(default_factory=list)

    assumptions: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    disclaimer: str = DISCLAIMER
    what_would_change: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _guard_gated_investing(self) -> MoneyPlan:
        """Refuse to build a plan that invests before the safety stack is done.

        This is the ordering invariant (``MONEY-RULES.md`` §3) expressed as a
        type constraint. A gated book cannot coexist with investable surplus or a
        Glider action; a positive investable surplus requires a real growth
        sleeve; and a Glider action requires a growth sleeve to hold. If a future
        refactor breaks the ordering, plan construction fails loudly instead of
        quietly recommending crypto to someone with 35% debt.
        """
        growth = self.book.growth_pct
        if (self.surplus_monthly > 0) != (growth > 0):
            raise ValueError(
                "investable surplus and a growth sleeve must appear together "
                f"(surplus={self.surplus_monthly}, growth={growth}%)"
            )
        if growth <= 0 and self.glider.kind != "none":
            raise ValueError(
                "a Glider action requires a growth sleeve; there is nothing to "
                "hold onchain without one"
            )
        if self.book.gated and growth > 0:
            raise ValueError("a safety-gated book cannot carry a growth sleeve")
        return self

    def is_investing(self) -> bool:
        """True only when the plan actually invests."""
        return not self.book.is_gated and self.surplus_monthly > 0
