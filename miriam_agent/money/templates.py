"""The three Miriam Glider books.

Data, not code: a template declares *what kind* of assets fill each sleeve and
the runtime resolves them against Glider's supported-asset list for the user's
chains. **No asset addresses are hardcoded.** A CAIP-19 address baked into a
source file is a dead contract waiting to happen, and it is exactly the kind of
"guess the asset id" failure the client is forbidden from making
(``MONEY-PLAN-CONTRACT.md`` §6).

Allocations are expressed as percent strings summing to 100, because that is
what ``POST /v2/strategies`` accepts.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from miriam_agent.money.formatting import weight_str
from miriam_agent.money.schema import AllocationBook

# Growth-share boundaries used to pick the closest book. Deliberately generous
# bands: the templates are a small set on purpose, and a 65% growth target
# should map to Core rather than invent a fourth template.
_BUILD_FLOOR = Decimal("75")
_CORE_FLOOR = Decimal("55")


class Schedule(BaseModel):
    """The strategy's rebalance cadence (Glider stores this separately)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["interval"] = "interval"
    frequency: Literal["daily", "weekly", "monthly"] = "monthly"

    def as_payload(self) -> dict[str, str]:
        return {"type": self.type, "frequency": self.frequency}


class SleeveSpec(BaseModel):
    """One slice of a sleeve, described by asset class rather than address."""

    model_config = ConfigDict(extra="forbid")

    sleeve: Literal["growth", "defensive"]
    asset_class: str
    # Share of the *sleeve*, not of the whole strategy.
    weight_of_sleeve: Decimal
    selection: str


class StrategyTemplate(BaseModel):
    """A reusable Miriam book, ready to be filled with validated assets."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    name: str
    growth_pct: Decimal
    defensive_pct: Decimal
    objective: str
    risk: str
    horizon: str
    schedule: Schedule = Field(default_factory=Schedule)
    sleeves: list[SleeveSpec] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _weights_sum(self) -> StrategyTemplate:
        total = self.growth_pct + self.defensive_pct
        if total != Decimal("100"):
            raise ValueError(f"{self.template_id} weights must sum to 100, got {total}")
        return self

    def sleeve_weight(self, sleeve: str) -> Decimal:
        return self.growth_pct if sleeve == "growth" else self.defensive_pct


def _growth_sleeves() -> list[SleeveSpec]:
    """Growth is one broad basket, not a pile of single names.

    ``R-BOGLE-2``: a single asset is not a sleeve. If a broad instrument is not
    available, the honest answer is to say so, not to substitute the shiniest
    individual token and call it diversification.
    """
    return [
        SleeveSpec(
            sleeve="growth",
            asset_class="broad_equity_index",
            weight_of_sleeve=Decimal("80"),
            selection=(
                "Prefer a broad, low-cost index trackers (tokenized equity index "
                "or target-date equivalent). Resolve at runtime from supported "
                "assets; do not hardcode an address."
            ),
        ),
        SleeveSpec(
            sleeve="growth",
            asset_class="tokenized_real_world_assets",
            weight_of_sleeve=Decimal("20"),
            selection=(
                "Tokenized real-world assets (e.g. treasuries or broad equity "
                "exposure) where the chain supports them."
            ),
        ),
    ]


def _defensive_sleeves() -> list[SleeveSpec]:
    """Defensive is cash-equivalent quality, and nothing volatile.

    A stablecoin belongs here only as a settlement instrument the user already
    understands -- never as an assumed 1:1 guarantee. Depeg risk is disclosed,
    not designed away.
    """
    return [
        SleeveSpec(
            sleeve="defensive",
            asset_class="high_quality_stablecoin",
            weight_of_sleeve=Decimal("70"),
            selection=(
                "A high-quality, widely-used stablecoin on a supported chain. "
                "Treat a depeg as a real loss, not a rounding error."
            ),
        ),
        SleeveSpec(
            sleeve="defensive",
            asset_class="tokenized_short_duration_paper",
            weight_of_sleeve=Decimal("30"),
            selection=(
                "Tokenized short-duration government paper where the chain "
                "supports it, as the closest cash-like instrument."
            ),
        ),
    ]


MIRIAM_CORE = StrategyTemplate(
    template_id="miriam_core_70_30",
    name="Miriam Core 70/30",
    growth_pct=Decimal("70"),
    defensive_pct=Decimal("30"),
    objective=(
        "Long-horizon growth with enough defence to survive a drawdown without "
        "selling"
    ),
    risk="medium",
    horizon="7+ years",
    schedule=Schedule(frequency="monthly"),
    sleeves=[*_growth_sleeves(), *_defensive_sleeves()],
    notes="The default book. Use when the horizon is 7+ years and income is steady.",
)

MIRIAM_PRESERVE = StrategyTemplate(
    template_id="miriam_preserve_40_60",
    name="Miriam Preserve 40/60",
    growth_pct=Decimal("40"),
    defensive_pct=Decimal("60"),
    objective="Keep pace with inflation without exposing money that is needed soon",
    risk="low",
    horizon="3-7 years, or low capacity",
    schedule=Schedule(frequency="monthly"),
    sleeves=[*_growth_sleeves(), *_defensive_sleeves()],
    notes=(
        "Use for a 3-7 year horizon, or when capacity is low (single income, "
        "dependents, variable pay). Also the conservative default when the "
        "horizon is unknown."
    ),
)

MIRIAM_BUILD = StrategyTemplate(
    template_id="miriam_build_80_20",
    name="Miriam Build 80/20",
    growth_pct=Decimal("80"),
    defensive_pct=Decimal("20"),
    objective="Maximum long-horizon growth for money that will not be touched",
    risk="high",
    horizon="15+ years",
    schedule=Schedule(frequency="monthly"),
    sleeves=[*_growth_sleeves(), *_defensive_sleeves()],
    notes=(
        "Only with a 15+ year horizon, high capacity, and a recorded acceptance "
        "of a 40% drawdown. This is the ceiling, not a target to grow into."
    ),
)

TEMPLATES: tuple[StrategyTemplate, ...] = (MIRIAM_PRESERVE, MIRIAM_CORE, MIRIAM_BUILD)

_BY_ID: dict[str, StrategyTemplate] = {t.template_id: t for t in TEMPLATES}


def get_template(template_id: str) -> StrategyTemplate | None:
    return _BY_ID.get(template_id)


def select_template(book: AllocationBook) -> StrategyTemplate | None:
    """The closest template for a book, or ``None`` when there is no book.

    ``None`` is a real answer, not a failure: a gated or short-horizon book has
    no Glider strategy, and the caller must handle that rather than being handed
    a default that quietly puts emergency money onchain.
    """
    if book.gated or book.short_horizon or book.growth_pct <= 0:
        return None
    if book.growth_pct >= _BUILD_FLOOR:
        return MIRIAM_BUILD
    if book.growth_pct >= _CORE_FLOOR:
        return MIRIAM_CORE
    return MIRIAM_PRESERVE


def template_weights(template: StrategyTemplate) -> dict[str, Decimal]:
    """Asset class -> percent of the whole strategy, summing to exactly 100.

    This is what the draft is made of, computed from the template alone so a
    draft can be built and inspected with no network and no resolved asset ids.
    Ids attach later, never in place of a weight.
    """
    weights: dict[str, Decimal] = {}
    for spec in template.sleeves:
        sleeve_pct = template.sleeve_weight(spec.sleeve)
        share = sleeve_pct * spec.weight_of_sleeve / Decimal("100")
        weights[spec.asset_class] = weights.get(spec.asset_class, Decimal("0")) + share
    return weights


def allocation_payload(
    template: StrategyTemplate, assets: dict[str, str]
) -> dict[str, object]:
    """Build the Glider strategy payload for a template.

    ``assets`` maps an ``asset_class`` to a resolved CAIP-19 ``assetId``. Any
    class the caller could not resolve is a hard error: fabricating an id, or
    silently dropping a sleeve so the weights no longer sum to 100, are both
    worse than refusing.
    """
    required = {spec.asset_class for spec in template.sleeves}
    missing = sorted(required - set(assets))
    if missing:
        raise ValueError(
            "cannot build a Glider strategy without resolved assets for: "
            + ", ".join(missing)
        )

    rows: list[dict[str, str]] = []
    for spec in template.sleeves:
        sleeve_pct = template.sleeve_weight(spec.sleeve)
        weight = sleeve_pct * spec.weight_of_sleeve / Decimal("100")
        rows.append(
            {
                "assetId": assets[spec.asset_class],
                "weight": weight_str(weight),
            }
        )

    # Weights are whole-percent strings that must sum to exactly 100. Rounding
    # per row can leave a remainder, so the largest row absorbs the difference
    # rather than the payload being rejected by the API.
    _rebalance_to_100(rows)
    return {
        "name": template.name,
        "description": template.objective,
        "allocation": {"assets": rows},
        "schedule": template.schedule.as_payload(),
    }


def _rebalance_to_100(rows: list[dict[str, str]]) -> None:
    total = sum((Decimal(r["weight"]) for r in rows), Decimal("0"))
    if total == Decimal("100"):
        return
    largest = max(rows, key=lambda r: Decimal(r["weight"]))
    largest["weight"] = weight_str(
        Decimal(largest["weight"]) + (Decimal("100") - total)
    )


def validate_weights(rows: list[dict[str, str]]) -> list[str]:
    """Local pre-flight check, mirroring Glider's own gates.

    The API is still the authority and ``POST /v2/strategies/validate`` is still
    called -- this only catches the obvious mistakes before spending a request.
    """
    problems: list[str] = []
    if not rows:
        return ["an allocation needs at least one asset"]
    if len(rows) > 50:
        problems.append("Glider allows at most 50 assets in an allocation")

    seen: set[str] = set()
    total = Decimal("0")
    for row in rows:
        asset_id = str(row.get("assetId") or "")
        if not asset_id:
            problems.append("an allocation row is missing its assetId")
            continue
        if asset_id in seen:
            problems.append(f"duplicate assetId: {asset_id}")
        seen.add(asset_id)
        if ":" not in asset_id or "/" not in asset_id:
            problems.append(f"assetId is not CAIP-19 shaped: {asset_id}")
        try:
            weight = Decimal(str(row.get("weight")))
        except (ArithmeticError, TypeError, ValueError):
            problems.append(f"weight is not a number: {row.get('weight')!r}")
            continue
        # Comparing against a 2-decimal quantisation is how "more than 2
        # decimals" is checked without reaching into the Decimal's exponent.
        if weight != weight.quantize(Decimal("0.01")):
            problems.append(f"weight has more than 2 decimals: {row.get('weight')}")
        if weight < 0:
            problems.append(f"weight is negative: {row.get('weight')}")
        total += weight

    if total != Decimal("100"):
        problems.append(f"allocation weights must sum to 100, got {total}")
    return problems
