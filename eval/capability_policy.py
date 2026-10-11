"""The bar each capability area must clear before a deploy ships.

Kept apart from the gate so a threshold change is a reviewable product
decision, not a tweak buried in runner code. Lowering any number here, or
removing an area from ``TRUST_AREAS``, should go through review like any other
policy change.
"""

from __future__ import annotations

# Areas where one wrong answer costs a trader real money or their trust in us.
# Every proven check in these areas must pass, every time.
TRUST_AREAS: frozenset[str] = frozenset(
    {
        "Payments & Fake Alerts",
        "Credit & Distributors",
        "Trust, Safety & Privacy",
        "Inventory",
        "Sales Records",
        "Expenses & Cash",
    }
)

TRUST_PASS_RATE = 1.0

# Everything else (language, advice, reports) is judged on a pass rate,
# because model output varies run to run.
QUALITY_PASS_RATE = 0.9


def required_pass_rate(area: str) -> float:
    return TRUST_PASS_RATE if area in TRUST_AREAS else QUALITY_PASS_RATE
