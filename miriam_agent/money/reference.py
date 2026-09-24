"""Curated local reference data: inflation, risk-free rates, debt thresholds.

The debt bands (``MONEY-RULES.md`` §3) and the inflation-erosion diagnosis both
need a local hurdle rate. Without one, 12% debt is either cheap or ruinous and
there is no way to tell which. This module supplies that number, and is honest
about how much it should be trusted.

Three honesty mechanisms, because a macro figure that looks official is worse
than one that admits it is a guess:

  1. **Every row carries an as-of date.** :data:`REFERENCE_AS_OF` is the day the
     table was pinned by hand, not a claim that the figures are live.
  2. **Every row is labelled ``sourced`` or PLACEHOLDER.** Nothing here is backed
     by a feed, so every row is currently a placeholder and says so in the plan's
     assumptions rather than reading as official.
  3. **Stale rows fail closed.** Past :data:`MAX_AGE_DAYS` the local rates are
     treated as unknown: investing is refused and the plan says why, so the
     table cannot silently rot into confident bad advice.

No yields are invented anywhere. These are planning assumptions with a review
date, and the mechanism is what is being built here; the figures are inputs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# The day the table below was pinned by hand. Refresh it, not the individual
# rows, when the figures are reviewed against a real source.
REFERENCE_AS_OF = "2026-09-19"

# After this many days the local rates are treated as unknown and investing is
# refused (fail closed). A stale hurdle rate is worse than no hurdle rate,
# because it still produces a confident answer.
MAX_AGE_DAYS = 90


def _today() -> date:
    return datetime.now(UTC).date()


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class SafetyVehicle(BaseModel):
    """Somewhere safe to park the buffer. Never Glider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str  # deposit | tbill | mmf
    insured: bool = False
    notes: str = ""


class TaxWrapper(BaseModel):
    """A tax-advantaged wrapper available in this country.

    Wrappers do not travel. ``R-AUDIT-4``: a 401(k) is not advice for a Lagos
    cash earner, and the plan must not imply it is.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    notes: str = ""


class CountryReference(BaseModel):
    """One country's planning assumptions.

    ``fire_apr_pct`` / ``judgment_apr_pct`` are the local debt thresholds. They
    are ``None`` on the fallback row so the global settings values apply to a
    country we have no data for; a real row overrides them, because a punitive
    rate in a high-rate currency is not the same number as one in a low-rate
    currency.
    """

    model_config = ConfigDict(extra="forbid")

    country_code: str
    currency: str
    as_of: str = REFERENCE_AS_OF
    inflation_pct: Decimal
    risk_free_rate_pct: Decimal
    fire_apr_pct: Decimal | None = None
    judgment_apr_pct: Decimal | None = None
    safety_vehicles: list[SafetyVehicle] = Field(default_factory=list)
    tax_wrappers: list[TaxWrapper] = Field(default_factory=list)
    # False means PLACEHOLDER: a hand-entered planning figure, not a sourced
    # observation. It is never presented to a user as if it were sourced.
    sourced: bool = False
    source_note: str = ""
    notes: str = ""

    def annual_erosion_pct(self) -> Decimal:
        """The rate at which idle cash loses purchasing power per year.

        Uses inflation against a conservative 0% deposit yield rather than an
        assumed deposit rate we cannot source. Overstating erosion would push
        people into risk they should not take, so the estimate stays
        deliberately unflattering to cash and no worse.
        """
        return max(Decimal("0"), self.inflation_pct)


class ReferenceStatus(BaseModel):
    """How much the reference row should be trusted, and why."""

    model_config = ConfigDict(extra="forbid")

    country_code: str
    currency: str
    as_of: str
    age_days: int
    matched: bool
    sourced: bool
    stale: bool
    note: str

    @property
    def trusted(self) -> bool:
        """A matched, fresh row. Placeholders are usable but downgrade confidence."""
        return self.matched and not self.stale


def reference_status(
    reference: CountryReference, *, today: date | None = None
) -> ReferenceStatus:
    """Evaluate a row's provenance, age and whether it may be relied on."""
    as_of = _parse_date(reference.as_of)
    current = today or _today()
    age_days = (current - as_of).days if as_of else MAX_AGE_DAYS + 1
    stale = age_days > MAX_AGE_DAYS
    return ReferenceStatus(
        country_code=reference.country_code,
        currency=reference.currency,
        as_of=reference.as_of,
        age_days=age_days,
        matched=reference.country_code in _TABLE,
        sourced=reference.sourced,
        stale=stale,
        note=_provenance(reference, age_days, stale),
    )


def _provenance(reference: CountryReference, age_days: int, stale: bool) -> str:
    """The one sentence that goes into ``MoneyPlan.assumptions``."""
    code = reference.country_code
    if reference.country_code not in _TABLE:
        return (
            f"no local reference for {code or 'an unknown country'}: inflation, "
            "risk-free rate and debt thresholds are placeholder assumptions and "
            "must be supplied or verified"
        )

    label = "sourced" if reference.sourced else "PLACEHOLDER"
    if reference.source_note:
        label = f"{label}: {reference.source_note}"
    note = (
        f"{reference.currency} reference figures are {label}, pinned "
        f"{reference.as_of} ({age_days} days old)"
    )
    if stale:
        note += (
            f". They are older than {MAX_AGE_DAYS} days, so the local rates are "
            "treated as unknown and investing is refused until the table is "
            "refreshed"
        )
    elif not reference.sourced:
        note += ". They are planning inputs, not a live feed; verify before acting"
    return note


def resolve_thresholds(
    reference: CountryReference, *, fire_default: Decimal, judgment_default: Decimal
) -> tuple[Decimal, Decimal]:
    """The debt bands for this country, falling back to the global settings.

    Per-country values win because a rate that counts as punitive in a low-rate
    currency is ordinary in a high-rate one. The settings value is the fallback
    for a country the table does not cover, which is why it is still live config
    rather than dead.
    """
    fire = (
        reference.fire_apr_pct if reference.fire_apr_pct is not None else fire_default
    )
    judgment = (
        reference.judgment_apr_pct
        if reference.judgment_apr_pct is not None
        else judgment_default
    )
    return fire, judgment


def _vehicle(name: str, kind: str, insured: bool, notes: str = "") -> SafetyVehicle:
    return SafetyVehicle(name=name, kind=kind, insured=insured, notes=notes)


def reference_from_env() -> CountryReference | None:
    """Operator-supplied live reference via environment (no code change).

    Reads ``MONEY_REF_*`` vars pinned in ``docs/MONEY-REFERENCE-OPS.md``::

        MONEY_REF_COUNTRY=NG MONEY_REF_CURRENCY=NGN
        MONEY_REF_INFLATION_PCT=24.0 MONEY_REF_RISK_FREE_PCT=19.0
        MONEY_REF_FIRE_APR_PCT=25.0 MONEY_REF_JUDGMENT_APR_PCT=10.0
        MONEY_REF_AS_OF=2026-09-24 MONEY_REF_SOURCE="CBN MPR release ..."

    Returns a ``sourced=True`` override or ``None`` when unset/incomplete, so
    the placeholder table keeps working until an operator wires the feed.
    """
    import os

    inflation = (os.environ.get("MONEY_REF_INFLATION_PCT") or "").strip()
    risk_free = (os.environ.get("MONEY_REF_RISK_FREE_PCT") or "").strip()
    if not inflation or not risk_free:
        return None
    try:
        from decimal import Decimal as _Decimal

        inflation_pct = _Decimal(inflation)
        risk_free_pct = _Decimal(risk_free)
    except Exception:
        return None
    fire = (os.environ.get("MONEY_REF_FIRE_APR_PCT") or "").strip()
    judgment = (os.environ.get("MONEY_REF_JUDGMENT_APR_PCT") or "").strip()
    try:
        fire_pct = _Decimal(fire) if fire else None
        judgment_pct = _Decimal(judgment) if judgment else None
    except Exception:
        return None
    return CountryReference(
        country_code=(os.environ.get("MONEY_REF_COUNTRY") or "NG").strip().upper(),
        currency=(os.environ.get("MONEY_REF_CURRENCY") or "NGN").strip().upper(),
        as_of=(os.environ.get("MONEY_REF_AS_OF") or REFERENCE_AS_OF).strip(),
        inflation_pct=inflation_pct,
        risk_free_rate_pct=risk_free_pct,
        fire_apr_pct=fire_pct,
        judgment_apr_pct=judgment_pct,
        safety_vehicles=list(_DEFAULT.safety_vehicles),
        tax_wrappers=[],
        sourced=True,
        source_note=(
            (os.environ.get("MONEY_REF_SOURCE") or "").strip()
            or "operator-supplied via MONEY_REF_* env"
        ),
    )


# ---------------------------------------------------------------------------
# The table. Every row is a PLACEHOLDER until someone fills in source_note.
# ---------------------------------------------------------------------------

_PLACEHOLDER = "hand-entered planning figure, not yet checked against a primary source"

_NG = CountryReference(
    country_code="NG",
    currency="NGN",
    inflation_pct=Decimal("24.0"),
    risk_free_rate_pct=Decimal("19.0"),
    fire_apr_pct=Decimal("25.0"),
    judgment_apr_pct=Decimal("10.0"),
    safety_vehicles=[
        _vehicle("Insured bank deposit (NDIC-covered)", "deposit", True),
        _vehicle(
            "FGN Treasury bill (short tenor)",
            "tbill",
            False,
            "Sovereign obligation; not deposit-insured",
        ),
        _vehicle(
            "Naira money-market fund",
            "mmf",
            False,
            "Check the fund's holdings before treating it as cash-like",
        ),
    ],
    tax_wrappers=[
        TaxWrapper(name="Contributory pension (PRA)", notes="Employer-linked only"),
        TaxWrapper(
            name="None broadly available",
            notes="No retail tax-advantaged account comparable to an ISA",
        ),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
    notes=(
        "High-inflation, high-local-rate currency. This is the case that breaks "
        "imported US advice: a 12% loan is cheap against a 19% risk-free rate, "
        "while cash still loses roughly 24% a year in purchasing power."
    ),
)

_US = CountryReference(
    country_code="US",
    currency="USD",
    inflation_pct=Decimal("3.0"),
    risk_free_rate_pct=Decimal("4.5"),
    fire_apr_pct=Decimal("15.0"),
    judgment_apr_pct=Decimal("8.0"),
    safety_vehicles=[
        _vehicle("FDIC-insured savings or deposit account", "deposit", True),
        _vehicle("US Treasury bill (short tenor)", "tbill", False),
        _vehicle("Government money-market fund", "mmf", False),
    ],
    tax_wrappers=[
        TaxWrapper(
            name="401(k)",
            notes="Employer-linked. Check for a match; never assume one exists",
        ),
        TaxWrapper(name="Roth IRA", notes="Contribution limits and income rules apply"),
        TaxWrapper(name="Taxable brokerage", notes="No wrapper limits"),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
    notes="US wrappers are country-specific and must never be applied elsewhere.",
)

_GB = CountryReference(
    country_code="GB",
    currency="GBP",
    inflation_pct=Decimal("3.0"),
    risk_free_rate_pct=Decimal("4.0"),
    fire_apr_pct=Decimal("15.0"),
    judgment_apr_pct=Decimal("8.0"),
    safety_vehicles=[
        _vehicle("FSCS-protected savings account", "deposit", True),
        _vehicle("UK gilt (short tenor)", "tbill", False),
        _vehicle("Sterling money-market fund", "mmf", False),
    ],
    tax_wrappers=[
        TaxWrapper(name="ISA", notes="Annual allowance applies"),
        TaxWrapper(name="SIPP", notes="Pension wrapper"),
        TaxWrapper(name="Workplace pension", notes="Check the employer match"),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
)

_KE = CountryReference(
    country_code="KE",
    currency="KES",
    inflation_pct=Decimal("7.0"),
    risk_free_rate_pct=Decimal("13.0"),
    fire_apr_pct=Decimal("20.0"),
    judgment_apr_pct=Decimal("12.0"),
    safety_vehicles=[
        _vehicle(
            "Bank or deposit-taking SACCO deposit",
            "deposit",
            True,
            "Confirm the institution is deposit-protected",
        ),
        _vehicle("Kenya Treasury bill (short tenor)", "tbill", False),
    ],
    tax_wrappers=[
        TaxWrapper(
            name="None broadly available",
            notes="No retail tax-advantaged investment account",
        ),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
)

_GH = CountryReference(
    country_code="GH",
    currency="GHS",
    inflation_pct=Decimal("23.0"),
    risk_free_rate_pct=Decimal("28.0"),
    fire_apr_pct=Decimal("30.0"),
    judgment_apr_pct=Decimal("20.0"),
    safety_vehicles=[
        _vehicle("Ghanaian bank deposit", "deposit", True),
        _vehicle("Ghana Treasury bill (short tenor)", "tbill", False),
    ],
    tax_wrappers=[
        TaxWrapper(
            name="Tier 3 voluntary pension", notes="Employer-linked schemes vary"
        ),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
    notes=(
        "High-inflation currency; local deposit rates can beat imported equity "
        "assumptions."
    ),
)

_ZA = CountryReference(
    country_code="ZA",
    currency="ZAR",
    inflation_pct=Decimal("5.0"),
    risk_free_rate_pct=Decimal("8.0"),
    fire_apr_pct=Decimal("18.0"),
    judgment_apr_pct=Decimal("10.0"),
    safety_vehicles=[
        _vehicle("South African bank deposit", "deposit", True),
        _vehicle("SA Treasury bill (short tenor)", "tbill", False),
    ],
    tax_wrappers=[
        TaxWrapper(name="TFSA", notes="Annual and lifetime limits apply"),
        TaxWrapper(name="Retirement annuity", notes="Pension wrapper"),
    ],
    sourced=False,
    source_note=_PLACEHOLDER,
)


# Fallback for a country we do not cover. Debt thresholds are left None so the
# global settings values apply -- a country we know nothing about should not get
# a confident local band. Inflation and the risk-free rate are deliberately
# unflattering, because being wrong in the direction of "park it somewhere safe
# and ask" is the survivable error.
_DEFAULT = CountryReference(
    country_code="XX",
    currency="USD",
    inflation_pct=Decimal("10.0"),
    risk_free_rate_pct=Decimal("10.0"),
    safety_vehicles=[
        _vehicle(
            "Insured local deposit account",
            "deposit",
            True,
            "Confirm the deposit-protection scheme applies",
        ),
    ],
    tax_wrappers=[],
    sourced=False,
    source_note="no local data; global default thresholds apply",
    notes=(
        "No reference entry for this country. Inflation and the risk-free rate "
        "are placeholder assumptions and must be supplied or verified."
    ),
)

_TABLE: dict[str, CountryReference] = {
    ref.country_code: ref for ref in (_NG, _US, _GB, _KE, _GH, _ZA)
}


def lookup(
    country: str | None,
    *,
    overrides: CountryReference | None = None,
    today: date | None = None,
) -> tuple[CountryReference, ReferenceStatus]:
    """The reference for ``country``, plus how far it should be trusted.

    ``overrides`` wins outright: a caller with the user's own stated deposit
    rate, or a live feed, is always better than this table.

    ``today`` is injectable so the staleness rule can be tested without waiting
    three months.
    """
    if overrides is not None:
        status = reference_status(overrides, today=today)
        return overrides, status.model_copy(
            update={
                "matched": True,
                "note": (
                    f"local rates supplied directly in {overrides.currency}; "
                    "reference table bypassed"
                ),
            }
        )

    code = (country or "").strip().upper()
    if code and code in _TABLE:
        ref = _TABLE[code]
        return ref, reference_status(ref, today=today)

    ref = _DEFAULT.model_copy(update={"country_code": code or _DEFAULT.country_code})
    status = reference_status(ref, today=today)
    return ref, status.model_copy(update={"matched": False})


def available_countries() -> list[str]:
    """The country codes the table actually covers."""
    return sorted(_TABLE)
