"""Run the money pipeline on a fixture and print what it produces.

    python -m miriam_agent.money.demo --fixture lagos
    python -m miriam_agent.money.demo --fixture us_surplus
    python -m miriam_agent.money.demo --fixture invest_it_all
    python -m miriam_agent.money.demo --all
    python -m miriam_agent.money.demo --fixture lagos --json

This is the product path in miniature: a profile in, a :class:`MoneyPlan` out,
spoken the way Miriam would say it plus the numbers behind it. No network, no
LLM, no database. If this stops working, the product is broken regardless of
what the chat layer does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from miriam_agent.money.formatting import format_amount, format_pct
from miriam_agent.money.plan import (
    GliderState,
    build_money_plan,
    render_plan,
    render_spoken,
)
from miriam_agent.money.schema import MoneyPlan

# Short names for the merge gates, mapped to the fixture files.
FIXTURES: dict[str, str] = {
    "lagos": "lagos_freelancer",
    "us_surplus": "us_w2_idle_cash",
    "invest_it_all": "invest_it_all",
    "bleed": "bleed",
    "fragile": "fragile_long_horizon",
    "drifted": "glider_drifted",
    "missing": "missing_income",
    "crypto": "crypto_everything",
}


def fixtures_dir() -> Path:
    """The shared fixture directory, resolved from the repo root."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "money"


def load_fixture(name: str) -> dict[str, Any]:
    key = FIXTURES.get(name, name)
    path = fixtures_dir() / f"{key}.json"
    if not path.exists():
        known = ", ".join(sorted(FIXTURES))
        raise SystemExit(f"no fixture '{name}' at {path}\nknown fixtures: {known}")
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def run_fixture(name: str) -> MoneyPlan:
    """Load a fixture and produce its plan. The one call the demo makes."""
    fixture = load_fixture(name)
    glider_state = None
    if fixture.get("portfolio") or fixture.get("positions"):
        glider_state = GliderState(
            portfolio=fixture.get("portfolio"),
            positions=fixture.get("positions") or [],
        )
    return build_money_plan(fixture["intake"], glider_state=glider_state)


def summarize(plan: MoneyPlan) -> str:
    """The operator's read of a plan: the numbers, then the next move."""
    cur = plan.currency
    split = plan.cashflow
    lines = [
        f"diagnosis      {plan.problem_type} ({plan.confidence})",
        f"spoken         {render_spoken(plan)}",
        f"take-home      {format_amount(plan.monthly_take_home, cur)}",
        f"split          fixed {format_amount(split.fixed, cur)} / "
        f"debt {format_amount(split.debt, cur)} / "
        f"savings {format_amount(split.savings, cur)} / "
        f"invest {format_amount(split.investments, cur)} / "
        f"guilt-free {format_amount(split.guilt_free, cur)}",
        f"buffer gap     {format_amount(plan.buffer.gap, cur)} of "
        f"{format_amount(plan.buffer.target_amount, cur)}",
        f"book           {format_pct(plan.book.growth_pct)} growth / "
        f"{format_pct(plan.book.defensive_pct)} defensive ({plan.book.rule_id})",
        f"surplus        {format_amount(plan.surplus_monthly, cur)} a month",
        f"glider         {plan.glider.kind}",
    ]
    if plan.glider.kind == "draft" and plan.glider.draft is not None:
        weights = ", ".join(
            f"{w.asset_class} {w.weight}%" for w in plan.glider.draft.weights
        )
        lines.append(f"  draft        {plan.glider.draft.name} [{weights}]")
        lines.append(
            f"  status       {plan.glider.draft.status}, submitted="
            f"{plan.glider.draft.submitted}"
        )
    elif plan.glider.kind == "monitor":
        lines.append(f"  portfolio    {plan.glider.portfolio_id}")
        for asset_id, delta in sorted(plan.glider.drift.items()):
            lines.append(f"  drift        {asset_id}: {delta:+.2f} pts")
    else:
        lines.append(f"  reason       {plan.glider.blocked_reason}")
    if plan.actions_90d:
        lines.append(f"next action    {plan.actions_90d[0].what}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m miriam_agent.money.demo",
        description="Run the money pipeline on a fixture.",
    )
    parser.add_argument("--fixture", help="fixture name, e.g. lagos")
    parser.add_argument(
        "--all", action="store_true", help="run every merge-gate fixture"
    )
    parser.add_argument(
        "--full", action="store_true", help="print the full structured plan"
    )
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)

    if args.all:
        names = ["lagos", "bleed", "invest_it_all", "us_surplus", "fragile", "drifted"]
    elif args.fixture:
        names = [args.fixture]
    else:
        parser.print_help()
        return 2

    failed = False
    for name in names:
        plan = run_fixture(name)
        if args.json:
            # Machine-readable mode prints only the plan, so the output can be
            # piped straight into jq.
            print(json.dumps(plan.model_dump(mode="json"), indent=2, default=str))
        else:
            print(f"=== {name} ===")
            print(render_plan(plan) if args.full else summarize(plan))
            print()
        if args.all:
            failed = _check_gate(name, plan) or failed

    return 1 if failed else 0


# The merge gates, asserted the same way the tests do. The demo failing loudly is
# the point: this is the path a user actually runs.
def _check_gate(name: str, plan: MoneyPlan) -> bool:
    if name in ("lagos", "bleed", "invest_it_all", "missing"):
        ok = not plan.is_investing() and plan.glider.kind == "none"
        why = "must not invest"
    elif name == "us_surplus":
        ok = plan.is_investing() and plan.glider.kind == "draft"
        why = "must reach a 70/30 draft"
    elif name == "fragile":
        ok = plan.book.growth_pct <= 50
        why = "capacity must be capped"
    elif name == "drifted":
        ok = plan.glider.kind == "monitor"
        why = "must monitor, not re-recommend"
    else:
        return False
    print(f"gate: {'PASS' if ok else 'FAIL'} ({why})")
    return not ok


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
