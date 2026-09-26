"""Stage 6 — assemble the plan, then render it. No LLM anywhere in this file.

:func:`build_money_plan` is the single money door: intake -> diagnose -> safety
stack -> cashflow -> book -> Glider decision -> :class:`MoneyPlan`.
:func:`explain_money_plan` exposes the same run as a reasoning trace for tests
and support. Both call one internal core, so the numbers cannot diverge.

Everything here is deterministic and pure, so the same user always gets the same
plan and a test can assert on it exactly. ``agent.py`` may rewrite the *wording*
of the diagnosis and the 90-day actions afterwards; it cannot change a number,
because the numbers are already decided by the time it sees them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.config.settings import get_settings
from miriam_agent.money.allocation import build_book
from miriam_agent.money.cashflow import build_cashflow, render_split
from miriam_agent.money.diagnose import Diagnosis, diagnose
from miriam_agent.money.formatting import format_amount, format_months, format_pct
from miriam_agent.money.glider import decide_glider
from miriam_agent.money.intake import (
    AccountsSnapshot,
    IntakeProfile,
    apply_accounts,
    from_financial_profile,
    from_mapping,
    from_onboarding_state,
)
from miriam_agent.money.reference import CountryReference, ReferenceStatus, lookup
from miriam_agent.money.safety import SafetyStack, assess_safety
from miriam_agent.money.schema import (
    DISCLAIMER,
    Action,
    AllocationBook,
    BufferPlan,
    CashflowSplit,
    Confidence,
    DebtAction,
    GliderAction,
    MoneyPlan,
)
from miriam_agent.money.text import scrub_voice


class GliderState(BaseModel):
    """What Glider already holds for this user, if anything.

    Feeding this in is what turns the Glider decision from "recommend a book"
    into "monitor the book you already have", so the pipeline does not tell
    someone with a live portfolio to go and open a new one.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio: dict | None = None
    positions: list[dict] = Field(default_factory=list)


class VaultState(BaseModel):
    """The Rail-owned locked dollar sleeve, when the user has one.

    The vault IS the long-horizon book for a vaulted user: the planner names
    the vault percent and unlock date in the surplus and automation lines and
    never drafts a second Miriam book (Core/Preserve/Build) on top of it. The
    vault tiers (Steady/Balanced/Growth) are Rail labels, not planner books,
    and the mix stays in the Rail YAML.
    """

    model_config = ConfigDict(extra="forbid")

    active: bool = False
    tier_label: str = ""
    vault_pct: Decimal = Decimal("0")
    unlock_date: str = ""
    total: Decimal | None = None


class Pipeline(BaseModel):
    """Every intermediate object, kept so a test (or a support engineer) can see
    how a plan was reached without re-running it."""

    model_config = ConfigDict(extra="forbid")

    intake: IntakeProfile
    reference: CountryReference
    status: ReferenceStatus
    diagnosis: Diagnosis
    safety: SafetyStack
    cashflow: CashflowSplit
    book: AllocationBook
    glider: GliderAction
    vault: VaultState = Field(default_factory=VaultState)


def coerce_profile(user_profile: Any) -> IntakeProfile:
    """Accept anything that describes a user, and normalize it to an intake.

    One door means one normalization step; without this, every caller would
    invent its own and the numbers would diverge. Accepts an ``IntakeProfile``,
    a ``FinancialProfile``, conversational onboarding state, or a plain dict.
    """
    from miriam_agent.financial.profile import FinancialProfile

    if isinstance(user_profile, IntakeProfile):
        return user_profile
    if isinstance(user_profile, FinancialProfile):
        return from_financial_profile(user_profile)
    if isinstance(user_profile, dict):
        return from_mapping(user_profile)
    if hasattr(user_profile, "learned") or hasattr(user_profile, "stage"):
        return from_onboarding_state(user_profile)
    raise TypeError(
        "build_money_plan expects an IntakeProfile, FinancialProfile, onboarding "
        f"state or dict; got {type(user_profile).__name__}"
    )


def _run_pipeline(
    intake: IntakeProfile,
    *,
    reference: CountryReference | None = None,
    glider_state: GliderState | dict | None = None,
    vault_state: VaultState | dict | None = None,
    today: date | None = None,
) -> Pipeline:
    """Run the pipeline in order. Internal: ``build_money_plan`` is the only door.

    Both public entrypoints below call this, so there is exactly one place the
    maths happens and no way for two callers to disagree.
    """
    if reference is None:
        # Operator-supplied live rates (MONEY_REF_* env) win over the placeholder
        # table without a code change; absent env keeps the table behavior.
        try:
            from miriam_agent.money.reference import reference_from_env

            reference = reference_from_env()
        except Exception:
            reference = None
    ref, status = lookup(intake.country, overrides=reference, today=today)
    diagnosis = diagnose(intake, reference=ref, status=status)
    safety = assess_safety(intake, ref, status=status)
    book = build_book(intake, safety, ref)
    vault = _coerce_vault(vault_state)
    glider_action = decide_glider(
        intake,
        safety,
        book,
        portfolio=_state_field(glider_state, "portfolio"),
        positions=_state_field(glider_state, "positions"),
        vault_active=vault.active,
    )
    split = build_cashflow(safety, book)
    return Pipeline(
        intake=intake,
        reference=ref,
        status=status,
        diagnosis=diagnosis,
        safety=safety,
        cashflow=split,
        book=book,
        glider=glider_action,
        vault=vault,
    )


def _state_field(state: GliderState | dict | None, field: str) -> Any:
    if state is None:
        return None if field == "portfolio" else []
    if isinstance(state, dict):
        return state.get(field)
    return getattr(state, field, None)


def _coerce_vault(state: VaultState | dict | None) -> VaultState:
    if state is None:
        return VaultState()
    if isinstance(state, VaultState):
        return state
    if isinstance(state, dict):
        data = dict(state)
        total = data.get("total")
        try:
            data["total"] = Decimal(str(total)) if total is not None else None
        except (ArithmeticError, TypeError, ValueError):
            data["total"] = None
        try:
            data["vault_pct"] = Decimal(str(data.get("vault_pct") or 0))
        except (ArithmeticError, TypeError, ValueError):
            data["vault_pct"] = Decimal("0")
        data["active"] = bool(data.get("active"))
        return VaultState(
            **{k: v for k, v in data.items() if k in VaultState.model_fields}
        )
    return VaultState()


def _vault_line(vault: VaultState) -> str:
    label = vault.tier_label or "locked dollar sleeve"
    pct = f"{vault.vault_pct:g} percent" if vault.vault_pct else "your set percent"
    unlock = f", opening {vault.unlock_date}" if vault.unlock_date else ""
    return f"locked dollar sleeve ({label} at {pct}{unlock})"


def explain_money_plan(
    user_profile: Any,
    accounts: AccountsSnapshot | dict | None = None,
    glider_state: GliderState | dict | None = None,
    vault_state: VaultState | dict | None = None,
    *,
    reference: CountryReference | None = None,
    today: date | None = None,
) -> Pipeline:
    """The reasoning trace behind a plan: every stage, not just the result.

    Diagnostics only. It delegates to the same core as
    :func:`build_money_plan`, so the numbers it shows are the numbers that ship.
    """
    intake = apply_accounts(coerce_profile(user_profile), accounts)
    return _run_pipeline(
        intake,
        reference=reference,
        glider_state=glider_state,
        vault_state=vault_state,
        today=today,
    )


def build_plan(pipeline: Pipeline) -> MoneyPlan:
    """Assemble the :class:`MoneyPlan` from a completed pipeline run.

    :class:`MoneyPlan`'s own validator re-checks the ordering invariant here, so
    a plan that would invest before the safety stack is done cannot be built.
    """
    stack = pipeline.safety
    book = pipeline.book
    currency = stack.currency

    debts = _debt_actions(stack, currency)
    buffer = BufferPlan(
        target_months=stack.buffer_target_months,
        target_amount=stack.buffer_target,
        current_amount=stack.buffer_current,
        gap=stack.buffer_gap,
        months_to_fill=stack.months_to_fill,
        vehicle=stack.buffer_vehicle,
        location=stack.buffer_location,
    )

    return MoneyPlan(
        diagnosis=pipeline.diagnosis.blunt,
        problem_type=pipeline.diagnosis.problem_type,
        currency=currency,
        monthly_take_home=stack.monthly_income,
        cashflow=pipeline.cashflow,
        buffer=buffer,
        debts=debts,
        surplus_monthly=book.investable_surplus,
        book=book,
        glider=pipeline.glider,
        actions_90d=_actions(pipeline, buffer, debts),
        automation_rules=_automation_rules(pipeline, buffer, debts),
        kill_switches=_kill_switches(pipeline, buffer),
        assumptions=_assumptions(pipeline),
        confidence=_confidence(pipeline),
        disclaimer=DISCLAIMER,
        what_would_change=_what_would_change(pipeline, buffer),
    )


def build_money_plan(
    user_profile: Any,
    accounts: AccountsSnapshot | dict | None = None,
    glider_state: GliderState | dict | None = None,
    vault_state: VaultState | dict | None = None,
    *,
    reference: CountryReference | None = None,
    today: date | None = None,
) -> MoneyPlan:
    """THE money door. Everything that needs a plan comes through here.

    ``user_profile`` is an ``IntakeProfile``, ``FinancialProfile``, onboarding
    state, or a plain dict. ``accounts`` are connected balances, which outrank
    anything stated by hand. ``glider_state`` is any existing Glider portfolio,
    which turns a recommendation into a monitoring decision. ``vault_state``
    is the Rail-owned locked dollar sleeve, which becomes the long-horizon
    book and suppresses any second Miriam draft on top.

    No hidden globals and no I/O: same inputs, same plan, every time. The LLM
    layer sits strictly on top of this and can only reword it.
    """
    intake = apply_accounts(coerce_profile(user_profile), accounts)
    return build_plan(
        _run_pipeline(
            intake,
            reference=reference,
            glider_state=glider_state,
            vault_state=vault_state,
            today=today,
        )
    )


# ---------------------------------------------------------------------------
# Derived sections
# ---------------------------------------------------------------------------


def _debt_actions(stack: SafetyStack, currency: str) -> list[DebtAction]:
    """The debts, with this month's extra attack assigned to the priority one.

    The extra goes to the first debt in priority order (already sorted: fire
    first, then the most expensive balance) -- splitting the attack across
    several balances is how both get cleared slowly.
    """
    actions = [d.model_copy(deep=True) for d in stack.debt_actions]
    remaining = stack.debt_extra
    for action in actions:
        if remaining <= 0:
            break
        if action.band in ("fire", "judgment"):
            applied = min(remaining, action.balance)
            action.extra_monthly = applied
            remaining -= applied
    return actions


def _actions(
    pipeline: Pipeline, buffer: BufferPlan, debts: list[DebtAction]
) -> list[Action]:
    """The 90-day plan: this week, this payday, this month.

    Written from the computed numbers, in the order the user should do them. Every
    amount here is a figure the pipeline produced.
    """
    intake = pipeline.intake
    stack = pipeline.safety
    book = pipeline.book
    currency = stack.currency
    actions: list[Action] = []

    if pipeline.diagnosis.problem_type == "data_gap":
        actions.append(
            Action(
                when="this week",
                what="Send me your take-home pay and your fixed monthly costs",
                currency=currency,
                how="Those two numbers are all I need to produce a real plan",
            )
        )
        return actions

    if stack.bleed:
        actions.append(
            Action(
                when="this week",
                what=(
                    "Write down every fixed outgoing, then find "
                    f"{format_amount(stack.bleed_gap, currency)} a month to cut or earn"
                ),
                amount=stack.bleed_gap,
                currency=currency,
                how="The month has to close before anything else in this plan matters",
            )
        )

    if buffer.gap > 0 and stack.buffer_contribution > 0:
        actions.append(
            Action(
                when="this payday",
                what=(
                    f"Move {format_amount(stack.buffer_contribution, currency)} "
                    "into the buffer"
                ),
                amount=stack.buffer_contribution,
                currency=currency,
                how=f"{buffer.vehicle}, {buffer.location}",
            )
        )

    worst = next((d for d in debts if d.extra_monthly > 0), None)
    if worst is not None:
        actions.append(
            Action(
                when="this payday",
                what=(
                    f"Pay the minimum everywhere, then put "
                    f"{format_amount(worst.extra_monthly, currency)} at the "
                    f"{worst.label} ({format_pct(worst.apr_pct, 1)} APR)"
                ),
                amount=worst.extra_monthly,
                currency=currency,
                how=worst.strategy,
            )
        )

    if book.investable_surplus > 0 and pipeline.glider.kind != "monitor":
        if pipeline.vault.active:
            actions.append(
                Action(
                    when="this month",
                    what=(
                        f"Keep funding the {_vault_line(pipeline.vault)} with "
                        f"{format_amount(book.investable_surplus, currency)} a month"
                    ),
                    amount=book.investable_surplus,
                    currency=currency,
                    how=(
                        "The locked sleeve is the long-horizon book, "
                        "approved in the app"
                    ),
                )
            )
        else:
            actions.append(
                Action(
                    when="this month",
                    what=(
                        f"Set up the {int(book.growth_pct)}/{int(book.defensive_pct)} "
                        f"book and fund it with "
                        f"{format_amount(book.investable_surplus, currency)} a month"
                    ),
                    amount=book.investable_surplus,
                    currency=currency,
                    how=(
                        "Broad, low-cost index exposure, funded "
                        "automatically on payday"
                    ),
                )
            )

    if pipeline.glider.kind == "monitor":
        # Someone who already holds a portfolio does not need a second one. They
        # need to know it has drifted and what the schedule will do about it.
        drifted = [
            asset for asset, delta in pipeline.glider.drift.items() if abs(delta) >= 5
        ]
        actions.append(
            Action(
                when="this month",
                what=(
                    f"Portfolio {pipeline.glider.portfolio_id} is off target on "
                    f"{len(drifted)} holding(s); let the schedule rebalance it, or "
                    "trigger one now"
                    if drifted
                    else f"Portfolio {pipeline.glider.portfolio_id} is on target; "
                    "leave the schedule alone"
                ),
                currency=currency,
                how=(
                    "Fund it with "
                    f"{format_amount(book.investable_surplus, currency)} a month "
                    "if you are still building"
                    if book.investable_surplus > 0
                    else "No new funding needed this month"
                ),
            )
        )

    if pipeline.glider.kind == "draft" and pipeline.glider.draft is not None:
        actions.append(
            Action(
                when="this month",
                what=(
                    f"Review the {pipeline.glider.draft.name} draft and sign it "
                    "yourself, or tell me what to change"
                ),
                currency=currency,
                how="Nothing is submitted and nothing is enrolled until you sign",
            )
        )

    if not book.gated and book.growth_pct > 0 and not intake.accepts_drawdown:
        actions.append(
            Action(
                when="this month",
                what=(
                    f"Decide now what you will do when the book falls "
                    f"{format_pct(get_settings().MONEY_DRAWDOWN_TOLERANCE_PCT)}"
                ),
                currency=currency,
                how=(
                    "Write it down before it happens; the plan only works if you "
                    "do not sell at the bottom"
                ),
            )
        )

    if not actions:
        actions.append(
            Action(
                when="this month",
                what="Keep the automation running and revisit in 90 days",
                currency=currency,
                how="Nothing here needs urgent action",
            )
        )
    return actions


def _automation_rules(
    pipeline: Pipeline, buffer: BufferPlan, debts: list[DebtAction]
) -> list[str]:
    """What moves on payday without anyone thinking about it (``R-BACH-1``)."""
    stack = pipeline.safety
    currency = stack.currency
    rules: list[str] = []

    if stack.buffer_contribution > 0:
        rules.append(
            f"On payday, move {format_amount(stack.buffer_contribution, currency)} "
            f"into {buffer.vehicle} before any spending"
        )
    worst = next((d for d in debts if d.extra_monthly > 0), None)
    if worst is not None:
        rules.append(
            f"On payday, send {format_amount(worst.extra_monthly, currency)} at "
            f"{worst.label} immediately after the buffer transfer"
        )
    if pipeline.book.investable_surplus > 0:
        if pipeline.vault.active:
            rules.append(
                "On payday, move "
                f"{format_amount(pipeline.book.investable_surplus, currency)} "
                f"into {_vault_line(pipeline.vault)}"
            )
        else:
            rules.append(
                "On payday, move "
                f"{format_amount(pipeline.book.investable_surplus, currency)} "
                f"into the {format_pct(pipeline.book.growth_pct)}/"
                f"{format_pct(pipeline.book.defensive_pct)} book"
            )

    rules.append(
        "Automate the transfers themselves, not the intention to make them -- a "
        "plan that depends on remembering it will not run"
    )
    return rules


def _kill_switches(pipeline: Pipeline, buffer: BufferPlan) -> list[str]:
    """When to stop. Written so a bad month has a trigger, not a debate."""
    settings = get_settings()
    stack = pipeline.safety
    currency = stack.currency
    essential = stack.monthly_fixed

    switches = [
        "Pause all new investing if the buffer falls below one month of "
        "essentials. Restart only when it is rebuilt.",
        f"Pause all new investing if any debt rate rises above "
        f"{format_pct(settings.MONEY_DEBT_FIRE_APR_PCT, 1)} APR.",
        f"If take-home drops below {format_amount(essential, currency)} a month, "
        "stop the discretionary line entirely and come back to me.",
    ]
    if pipeline.book.growth_pct > 0:
        switches.append(
            "If you cannot leave the invested money alone for the full horizon, "
            "the money is in the wrong place -- tell me before you sell."
        )
    if pipeline.glider.kind != "none":
        switches.append(
            "Exit the onchain book if a stablecoin depegs, or if you need the "
            "money inside twelve months. A drawdown is not a reason to sell; "
            "needing the money is."
        )
    return switches


def _assumptions(pipeline: Pipeline) -> list[str]:
    """Every labelled assumption, deduplicated, in a stable order."""
    seen: dict[str, None] = {}
    for note in (
        *pipeline.intake.assumptions,
        *pipeline.diagnosis.assumptions,
        *pipeline.safety.assumptions,
        *pipeline.book.overrides,
        pipeline.status.note,
    ):
        if note and note not in seen:
            seen[note] = None
    return list(seen)


def _confidence(pipeline: Pipeline) -> Confidence:
    """The plan is only as confident as the diagnosis under it.

    Downgraded further when a load-bearing figure is a placeholder rather than a
    fact. A plan built on an unreviewed local inflation figure is not a
    high-confidence plan, whatever the inputs looked like.
    """
    base = pipeline.diagnosis.confidence
    status = pipeline.status
    if status.stale:
        return "low"
    shaky = not status.sourced or not status.matched
    if base == "high" and shaky:
        return "medium"
    if base == "medium" and shaky:
        return "low"
    return base


def _what_would_change(pipeline: Pipeline, buffer: BufferPlan) -> list[str]:
    """The specific, checkable things that would make this a different plan."""
    settings = get_settings()
    stack = pipeline.safety
    currency = stack.currency
    changes: list[str] = []

    if stack.fire_debt or stack.judgment_debt:
        worst = next((d for d in stack.debt_actions if d.apr_pct > 0), None)
        if worst is not None:
            changes.append(
                f"Clearing the {worst.label} at {format_pct(worst.apr_pct, 1)} APR "
                "would free up the whole debt attack and move investing forward"
            )
    if buffer.gap > 0:
        changes.append(
            f"Reaching the {format_amount(buffer.target_amount, currency)} buffer "
            f"target would complete the safety stack"
        )
    if stack.bleed:
        changes.append(
            f"Closing the {format_amount(stack.bleed_gap, currency)} monthly gap, "
            "by cutting costs or raising income"
        )
    changes.append(
        "A change in income -- up or down -- changes the buffer target and the "
        "book, so tell me when it moves"
    )
    horizon = pipeline.intake.horizon_months
    changes.append(
        f"The goal horizon moving past three years would allow market exposure; "
        f"it is currently {horizon} months"
        if horizon is not None and horizon < 36
        else "A shorter goal horizon would move this money out of markets and "
        "into cash-like instruments"
    )
    changes.append(
        f"If {pipeline.reference.currency} inflation or the local risk-free rate "
        f"moves materially from the {pipeline.reference.as_of} reference figures, "
        "the debt judgment band and the inflation read both change"
    )
    if pipeline.book.growth_pct >= 70 and not pipeline.intake.accepts_drawdown:
        changes.append(
            f"A recorded acceptance of a "
            f"{format_pct(settings.MONEY_DRAWDOWN_TOLERANCE_PCT)} drawdown would "
            "allow a more growth-weighted book"
        )
    return changes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan(plan: MoneyPlan) -> str:
    """Render the 12-part structure. Direct, specific, no encouragement."""
    cur = plan.currency
    lines: list[str] = []

    lines.append("1. DIAGNOSIS")
    lines.append(plan.diagnosis)
    lines.append("")

    lines.append("2. REALITY CHECK")
    for line in _reality_check(plan):
        lines.append(f"  - {line}")
    lines.append("")

    lines.append("3. 90-DAY PLAN")
    if plan.actions_90d:
        for action in plan.actions_90d:
            lines.append(f"  - {action.when}: {action.what}")
    else:
        lines.append("  - nothing outstanding")
    lines.append("")

    lines.append("4. CASHFLOW SPLIT")
    for line in render_split(plan.cashflow, plan.monthly_take_home):
        lines.append(f"  {line}")
    if plan.cashflow.note:
        lines.append(f"  note: {plan.cashflow.note}")
    lines.append("")

    lines.append("5. DEBT ACTIONS")
    if not plan.debts:
        lines.append("  none recorded")
    for debt in plan.debts:
        extra = (
            f", plus {format_amount(debt.extra_monthly, cur)} this month"
            if debt.extra_monthly > 0
            else ""
        )
        lines.append(
            f"  - {debt.label}: {format_amount(debt.balance, cur)} at "
            f"{format_pct(debt.apr_pct, 1)} APR ({debt.band}), minimum "
            f"{format_amount(debt.minimum_monthly, cur)}{extra}"
        )
        lines.append(f"    {debt.reason}")
    lines.append("")

    lines.append("6. BUFFER")
    lines.append(
        f"  target {format_amount(plan.buffer.target_amount, cur)} "
        f"({format_months(plan.buffer.target_months)} of essentials), "
        f"held now {format_amount(plan.buffer.current_amount, cur)}, "
        f"gap {format_amount(plan.buffer.gap, cur)}"
    )
    lines.append(f"  vehicle: {plan.buffer.vehicle}, {plan.buffer.location}")
    if plan.buffer.months_to_fill is not None:
        lines.append(
            f"  at the current pace: {format_months(plan.buffer.months_to_fill)}"
        )
    lines.append("")

    lines.append("7. INVESTABLE SURPLUS AND BOOK")
    if not plan.is_investing():
        lines.append(
            f"  none. Investable surplus is {format_amount(Decimal('0'), cur)} "
            "while the safety stack is incomplete."
        )
        if plan.book.reason:
            lines.append(f"  {plan.book.reason}")
    else:
        lines.append(
            f"  investable surplus {format_amount(plan.surplus_monthly, cur)} a month"
        )
        lines.append(
            f"  book {format_pct(plan.book.growth_pct)} growth / "
            f"{format_pct(plan.book.defensive_pct)} defensive ({plan.book.rule_id})"
        )
        lines.append(f"  why: {plan.book.reason}")
        for override in plan.book.overrides:
            lines.append(f"  override: {override}")
        for item in plan.book.growth_sleeve:
            lines.append(f"  growth: {item}")
        for item in plan.book.defensive_sleeve:
            lines.append(f"  defensive: {item}")
    lines.append("")

    lines.append("8. GLIDER ACTION")
    if plan.glider.kind == "none":
        lines.append("  none, nothing goes onchain")
        if plan.glider.blocked_reason:
            lines.append(f"  {plan.glider.blocked_reason}")
    elif plan.glider.kind == "draft" and plan.glider.draft is not None:
        draft = plan.glider.draft
        lines.append(f"  draft: {draft.name} ({draft.book})")
        lines.append(f"  status: {draft.status}, submitted: {draft.submitted}")
        for weight in draft.weights:
            resolved = weight.asset_id or "asset id not resolved (offline)"
            lines.append(f"    {weight.asset_class}: {weight.weight}% -> {resolved}")
        lines.append(f"  schedule: {draft.schedule}")
        if draft.note:
            lines.append(f"  note: {draft.note}")
        lines.append("  you sign the enrollment yourself; nothing is submitted")
    else:
        lines.append(f"  monitor portfolio {plan.glider.portfolio_id}")
        if plan.glider.drift:
            for asset_id, delta in sorted(plan.glider.drift.items()):
                lines.append(f"    {asset_id}: {delta:+.2f} points vs target")
    if plan.glider.risks and plan.glider.kind != "none":
        lines.append("  risks you are accepting:")
        for risk in plan.glider.risks:
            lines.append(f"    - {risk}")
    lines.append("")

    lines.append("9. AUTOMATION")
    for rule in plan.automation_rules:
        lines.append(f"  - {rule}")
    lines.append("")

    lines.append("10. KILL SWITCHES")
    for switch in plan.kill_switches:
        lines.append(f"  - {switch}")
    lines.append("")

    lines.append(f"11. ASSUMPTIONS AND CONFIDENCE ({plan.confidence})")
    for note in plan.assumptions:
        lines.append(f"  - {note}")
    lines.append("")

    lines.append("12. WHAT WOULD CHANGE THIS PLAN")
    for change in plan.what_would_change:
        lines.append(f"  - {change}")
    lines.append("")

    lines.append(plan.disclaimer)
    return scrub_voice("\n".join(lines))


def _reality_check(plan: MoneyPlan) -> list[str]:
    """What the numbers actually say, stated flatly."""
    cur = plan.currency
    split = plan.cashflow
    lines = [
        f"take-home {format_amount(plan.monthly_take_home, cur)} a month",
        f"fixed costs {format_amount(split.fixed, cur)}, "
        f"guilt-free spending {format_amount(split.guilt_free, cur)}, "
        f"debt attack {format_amount(split.debt, cur)}",
    ]
    if plan.debts:
        total = sum((d.balance for d in plan.debts), Decimal("0"))
        lines.append(f"debt outstanding {format_amount(total, cur)}")
    else:
        lines.append("no debt on record")
    lines.append(
        f"buffer {format_amount(plan.buffer.current_amount, cur)} against a "
        f"{format_amount(plan.buffer.target_amount, cur)} target"
    )
    lines.append(
        f"investable surplus {format_amount(plan.surplus_monthly, cur)} a month"
    )
    return lines


# Chat default: the diagnosis and one next move, kept inside a sane length so
# the plan never arrives as a wall of text. Plan mode uses ``render_plan``.
_SPOKEN_MAX_WORDS = 60


def render_spoken(plan: MoneyPlan) -> str:
    """The short form Miriam says in chat: diagnosis plus one next move.

    Aimed at 15 to 60 words. If the diagnosis alone overruns, the extra lines are
    dropped rather than truncated mid-sentence, because a half-sentence about
    money is worse than a short one.
    """
    cur = plan.currency
    head = [plan.diagnosis.strip()]

    if plan.is_investing():
        head.append(
            f"Book is {int(plan.book.growth_pct)}/{int(plan.book.defensive_pct)} "
            f"({format_amount(plan.surplus_monthly, cur)} a month)."
        )
    elif plan.buffer.gap > 0:
        head.append(f"Buffer gap is {format_amount(plan.buffer.gap, cur)}.")

    next_move = ""
    if plan.actions_90d:
        next_move = f"Next: {plan.actions_90d[0].what}."

    text = " ".join([*head, next_move])
    if len(text.split()) > _SPOKEN_MAX_WORDS:
        text = " ".join([plan.diagnosis.strip(), next_move])
    return scrub_voice(" ".join(text.split()))


def pipeline_to_dict(pipeline: Pipeline) -> dict[str, Any]:
    """A JSON-serializable dump of every stage, for tracing and evals."""
    return {
        "intake": pipeline.intake.model_dump(mode="json"),
        "reference": pipeline.reference.model_dump(mode="json"),
        "status": pipeline.status.model_dump(mode="json"),
        "diagnosis": pipeline.diagnosis.model_dump(mode="json"),
        "safety": pipeline.safety.model_dump(mode="json"),
        "cashflow": pipeline.cashflow.model_dump(mode="json"),
        "book": pipeline.book.model_dump(mode="json"),
        "glider": pipeline.glider.model_dump(mode="json"),
    }


__all__ = [
    "GliderState",
    "Pipeline",
    "build_money_plan",
    "build_plan",
    "coerce_profile",
    "explain_money_plan",
    "pipeline_to_dict",
    "render_plan",
    "render_spoken",
]
