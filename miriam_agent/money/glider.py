"""Stage 5 - the Glider *decision*, as a draft rather than a promise.

Which book, whether it is allowed at all, and how far a live portfolio has
drifted from the approved target. The wire goes through the Go money/ledger
host (``/api/v1/investments/*``), which holds the only x-api-key; this module
never makes a request and there is no direct Glider client in this repo.

A Glider action requires **all** of:

  - the safety stack cleared (``investing_allowed``);
  - a real horizon (not short-horizon cash-like money);
  - the user can self-custody it;
  - a book with a growth sleeve to hold.

Anything else returns ``kind="none"`` with a reason, and the model requires that
reason to exist, so the plan can explain the refusal in the user's own terms.

The draft is a complete proposal: a name, weights that sum to exactly 100, and a
schedule. It is built from the template alone, with **no asset ids**, because
resolving a CAIP-19 id needs a live source and guessing one is how money ends up
in a token nobody chose. Ids attach later, from a real source, or they do not
attach at all.

Two absolute rules are encoded here rather than left to convention:

  - **Emergency funds never go onchain.** Only ``investable_surplus`` is ever
    sized for Glider, and ``BufferPlan`` cannot represent a Glider-held buffer.
  - **Never auto-enroll.** A draft is a draft. ``submitted`` is pinned false by
    the schema, and stage-2 enrollment is not implemented in the client at all.
"""

from __future__ import annotations

from decimal import Decimal

from miriam_agent.money.formatting import join_sentences
from miriam_agent.money.intake import IntakeProfile
from miriam_agent.money.safety import SafetyStack
from miriam_agent.money.schema import (
    GLIDER_RISKS,
    AllocationBook,
    DraftWeight,
    GliderAction,
    GliderDraft,
)
from miriam_agent.money.templates import (
    StrategyTemplate,
    select_template,
    template_weights,
)

# Below this, a drift is normal market noise rather than something worth acting
# on. Acting on every wobble is how a rebalance schedule turns into churn.
_DRIFT_NOTICE_POINTS = Decimal("5")


def build_draft(
    template: StrategyTemplate,
    *,
    resolved_assets: dict[str, str] | None = None,
    status: str = "draft_local",
    note: str = "",
) -> GliderDraft:
    """Turn a template into a complete draft, with or without resolved ids.

    ``resolved_assets`` maps ``asset_class`` to a CAIP-19 id discovered from a
    real source. Absent, the draft is still complete on weights and says so; it
    simply is not submit-ready, and ``as_payload`` will refuse it rather than
    invent an id.
    """
    resolved = resolved_assets or {}
    weights = [
        DraftWeight(
            asset_class=asset_class,
            weight=weight,
            asset_id=resolved.get(asset_class, ""),
        )
        for asset_class, weight in template_weights(template).items()
    ]
    # Order the draft the way a person reads it: growth first, then defence.
    growth_classes = {s.asset_class for s in template.sleeves if s.sleeve == "growth"}
    weights.sort(key=lambda w: (w.asset_class not in growth_classes, -w.weight))

    if not note:
        unresolvable = [w.asset_class for w in weights if not w.asset_id]
        note = (
            "weights are final; asset ids still need resolving from a live source "
            f"for: {', '.join(unresolvable)}"
            if unresolvable
            else "weights and asset ids are both resolved and ready to validate"
        )

    return GliderDraft(
        name=template.name,
        template=template.template_id,
        book=f"{int(template.growth_pct)}/{int(template.defensive_pct)}",
        weights=weights,
        schedule=template.schedule.as_payload(),
        status=status,  # type: ignore[arg-type]
        submitted=False,
        note=note,
    )


def decide_glider(
    intake: IntakeProfile,
    stack: SafetyStack,
    book: AllocationBook,
    *,
    portfolio: dict | None = None,
    positions: list[dict] | None = None,
    template: StrategyTemplate | None = None,
    resolved_assets: dict[str, str] | None = None,
    validation: str = "",
) -> GliderAction:
    """Decide what to do about Glider, and say why when the answer is nothing."""
    chosen = template or select_template(book)

    # -- refusals, in the order a user would want them explained --------
    if book.gated:
        return GliderAction(
            kind="none",
            blocked_reason=join_sentences(
                [
                    "Nothing goes onchain yet",
                    *stack.blocked_reasons,
                    "Onchain money can fail exactly when you need it, so the "
                    "buffer and the debt come first",
                ]
            ),
            risks=list(GLIDER_RISKS),
        )

    if book.short_horizon or book.investable_surplus <= 0:
        return GliderAction(
            kind="none",
            blocked_reason=(
                "There is no investable surplus for an onchain strategy: this "
                "money is needed too soon to absorb a drawdown, so it stays in "
                "cash-like instruments."
            ),
            risks=list(GLIDER_RISKS),
        )

    if intake.can_self_custody is not True:
        return GliderAction(
            kind="none",
            blocked_reason=(
                "This needs you to hold your own keys. Non-custodial means the "
                "keys are yours, and a lost key is a lost balance with no "
                "support line that can undo it. Say the word and I will walk "
                "through what that involves before anything moves."
            ),
            risks=list(GLIDER_RISKS),
        )

    if chosen is None:
        return GliderAction(
            kind="none",
            blocked_reason="No book applies to this surplus.",
            risks=list(GLIDER_RISKS),
        )

    # -- monitoring an existing portfolio --------------------------------
    if portfolio:
        portfolio_id = str(portfolio.get("portfolioId") or portfolio.get("id") or "")
        return GliderAction(
            kind="monitor",
            template=chosen.template_id,
            portfolio_id=portfolio_id,
            validation=validation,
            drift=drift_from_target(
                positions or [],
                target_weights(chosen, portfolio.get("targetAllocation") or {}),
            ),
            risks=list(GLIDER_RISKS),
        )

    # -- a draft the user inspects and signs themselves ------------------
    return GliderAction(
        kind="draft",
        template=chosen.template_id,
        draft=build_draft(chosen, resolved_assets=resolved_assets),
        validation=validation,
        drift={},
        risks=list(GLIDER_RISKS),
    )


def target_weights(
    template: StrategyTemplate, resolved: dict[str, str]
) -> dict[str, Decimal]:
    """Target weights per asset id, from a template plus resolved assets.

    ``resolved`` maps ``asset_class`` -> CAIP-19 ``assetId``. Returns an empty
    map when nothing resolved, so a caller compares against a target it actually
    has rather than against a guess.
    """
    if not resolved:
        return {}
    weights: dict[str, Decimal] = {}
    for asset_class, share in template_weights(template).items():
        asset_id = resolved.get(asset_class)
        if not asset_id:
            continue
        weights[asset_id] = weights.get(asset_id, Decimal("0")) + share
    return weights


def drift_from_target(
    positions: list[dict], targets: dict[str, Decimal]
) -> dict[str, float]:
    """How far live positions sit from target, in percentage points.

    Positive means overweight, negative underweight. Values come from the
    positions payload Glider returns (``valueUsd``); when the portfolio total is
    zero the drift is reported as empty rather than as a fabricated 100%.
    """
    if not positions:
        return {}

    values: dict[str, Decimal] = {}
    total = Decimal("0")
    for row in positions:
        asset_id = str(row.get("assetId") or "")
        raw = row.get("valueUsd")
        if not asset_id or raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            continue
        values[asset_id] = values.get(asset_id, Decimal("0")) + value
        total += value

    if total <= 0:
        return {}

    drift: dict[str, float] = {}
    for asset_id in sorted(set(values) | set(targets)):
        current = (values.get(asset_id, Decimal("0")) / total) * Decimal("100")
        target = targets.get(asset_id, Decimal("0"))
        drift[asset_id] = float((current - target).quantize(Decimal("0.01")))
    return drift


def needs_rebalance(drift: dict[str, float]) -> bool:
    """Whether a drift is worth acting on, rather than normal noise."""
    return any(abs(Decimal(str(v))) >= _DRIFT_NOTICE_POINTS for v in drift.values())
