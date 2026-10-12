"""Trust-rail regression tests: grounding, limits tiers, execution assertion.

These encode the acceptance scenarios for "Miriam is someone I can trust with
real money": an asset never resolves to an invented ticker, a proposed movement
always lands in exactly one of ALLOW / CONFIRM / BLOCK, and a completion message
can only narrate what a receipt actually did. All deterministic -- no network,
no LLM, no JEV.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from layer_fakes import ledger_with

from miriam_agent.hands.assets import resolve_asset
from miriam_agent.hands.audit import Receipt
from miriam_agent.hands.execution_claim import (
    execution_facts,
    verify_execution_narration,
)
from miriam_agent.hands.ledger import Ledger, money
from miriam_agent.hands.limits import (
    LimitDecision,
    LimitVerdict,
    Policy,
    classify_limits,
)

NOW = datetime.now(UTC)


def _policy(**overrides) -> Policy:
    defaults = dict(
        max_auto=money(100),  # below this: autonomous
        max_with_confirm=money(50000),  # above this: hard block
        reversible_under=money(100),
        max_daily=money(250000),
        max_weekly=money(500000),
        max_per_hour=20,
    )
    defaults.update(overrides)
    return Policy(**defaults)


def _classify(
    amount, *, policy=None, sleeve="spendable", spendable=1000000, ledger=None, at=NOW
) -> LimitDecision:
    return classify_limits(
        policy=policy or _policy(),
        amount=money(amount),
        sleeve=sleeve,
        spendable=money(spendable),
        rent_required=money(0),
        rent_reserved=money(0),
        ledger=ledger,
        at=at,
    )


def _receipt(
    ledger: Ledger,
    *,
    amount,
    action="transfer",
    status="executed",
    at=NOW,
    currency="USD",
) -> Receipt:
    receipt = Receipt(
        id=f"rcpt_{len(ledger.receipts)}",
        at=at,
        status=status,
        action=action,
        currency=currency,
        amount=money(amount),
        counterparty="counterparty",
        sleeve="spendable",
    )
    ledger.remember_receipt(receipt)
    return receipt


# ---------------------------------------------------------------------------
# Grounding: resolve_asset
# ---------------------------------------------------------------------------


def test_google_resolves_to_googl_not_an_invented_ticker():
    r = resolve_asset("google")
    assert r.kind == "resolved"
    assert r.asset.symbol == "GOOGL"
    assert r.asset.currency == "USD"
    assert r.asset.supported is False  # single-name is not a live product


def test_bare_ticker_resolves_case_insensitively():
    for q in ("GOOGL", "googl", "  GoOgL  "):
        r = resolve_asset(q)
        assert r.kind == "resolved"
        assert r.asset.symbol == "GOOGL"


def test_uncatalogued_ticker_is_not_silently_resolved():
    # resolve_asset covers the curated company catalogue; a ticker outside it is
    # unknown (the order path validates arbitrary tickers against Go, not here).
    assert resolve_asset("goog").kind == "unknown"


def test_misspelling_is_unknown_never_guessed():
    r = resolve_asset("gogle")
    assert r.kind == "unknown"
    assert r.asset is None


def test_alphabet_resolves_to_googl_like_the_symbol_parser():
    # The product maps "alphabet" (and "google") to GOOGL, Class A. The resolve
    # gate must agree with nl._parse_symbol, not invent a Class C alternative.
    r = resolve_asset("alphabet")
    assert r.resolved and r.asset.symbol == "GOOGL"


def test_genuine_ambiguity_asks_rather_than_guessing():
    # A custom catalogue where one alias points at two assets must surface as
    # an ask, never a silent pick.
    from miriam_agent.hands.assets import Asset

    a = Asset(symbol="AAA", name="Twin A", currency="USD", aliases=("twinco",))
    b = Asset(symbol="BBB", name="Twin B", currency="USD", aliases=("twinco",))
    r = resolve_asset("twinco", catalogue=(a, b))
    assert r.kind == "ambiguous"
    assert {x.symbol for x in r.candidates} == {"AAA", "BBB"}


def test_company_aliases_match_the_symbol_parser():
    # Drift guard: nl.py derives its company map from assets.company_aliases(),
    # so the catalogue and the symbol parser can never disagree.
    from miriam_agent.hands.assets import company_aliases
    from miriam_agent.hands.orders import _parse_symbol

    for name, symbol in company_aliases().items():
        assert _parse_symbol(f"buy 5 {name}") == f"{symbol}x"


def test_empty_query_is_unknown():
    assert resolve_asset("").kind == "unknown"


def test_company_name_resolves():
    assert resolve_asset("apple").asset.symbol == "AAPL"
    assert resolve_asset("microsoft").asset.symbol == "MSFT"


# ---------------------------------------------------------------------------
# Limits tiers: classify_limits
# ---------------------------------------------------------------------------


def test_small_amount_is_allow():
    d = _classify(20)
    assert d.verdict is LimitVerdict.ALLOW
    assert d.confirm_needed is False


def test_above_auto_but_within_caps_is_confirm():
    d = _classify(500)  # > max_auto(100), < max_with_confirm(50000)
    assert d.verdict is LimitVerdict.CONFIRM
    assert d.confirm_needed is True


def test_above_transaction_cap_is_block():
    d = _classify(60000)  # > max_with_confirm(50000)
    assert d.verdict is LimitVerdict.BLOCK
    assert "OVER_LIMIT" in d.report.reasons


def test_non_positive_amount_is_block():
    d = _classify(-20)
    assert d.verdict is LimitVerdict.BLOCK
    assert "NON_POSITIVE_AMOUNT" in d.report.reasons


def test_zero_amount_is_block():
    d = _classify(0)
    assert d.verdict is LimitVerdict.BLOCK


def test_locked_sleeve_is_block():
    d = _classify(20, sleeve="locked")
    assert d.verdict is LimitVerdict.BLOCK
    assert "LOCKED_SLEEVE" in d.report.reasons


def test_over_balance_is_block():
    d = _classify(200, spendable=150)
    assert d.verdict is LimitVerdict.BLOCK
    assert "OVER_BALANCE" in d.report.reasons


def test_daily_cap_is_block():
    ledger = ledger_with()
    _receipt(ledger, amount=250000)  # already at the daily cap
    d = _classify(1, ledger=ledger)
    assert d.verdict is LimitVerdict.BLOCK
    assert "DAILY_CAP" in d.report.reasons


def test_weekly_cap_is_block():
    ledger = ledger_with()
    # Within the current week, and the daily cap is raised out of the way so
    # only the weekly cap can fire.
    _receipt(ledger, amount=500000, at=NOW)
    d = _classify(
        1,
        policy=_policy(max_daily=money(10**9), max_weekly=money(500000)),
        ledger=ledger,
    )
    assert d.verdict is LimitVerdict.BLOCK
    assert "WEEKLY_CAP" in d.report.reasons


def test_velocity_is_block():
    ledger = ledger_with()
    for i in range(20):  # max_per_hour == 20 already settled this hour
        _receipt(ledger, amount=1, at=NOW - timedelta(minutes=i))
    d = _classify(1, ledger=ledger)
    assert d.verdict is LimitVerdict.BLOCK
    assert "VELOCITY" in d.report.reasons


def test_velocity_under_limit_is_not_blocked():
    ledger = ledger_with()
    for i in range(19):
        _receipt(ledger, amount=1, at=NOW - timedelta(minutes=i))
    d = _classify(1, ledger=ledger)
    assert "VELOCITY" not in d.report.reasons


# ---------------------------------------------------------------------------
# Execution assertion (no hallucinated "done!")
# ---------------------------------------------------------------------------


def _executed_receipt(amount="20", currency="USD", counterparty="Rail Stock Sleeve"):
    return Receipt(
        id="rcpt_x",
        at=NOW,
        status="executed",
        action="invest_settle",
        currency=currency,
        amount=money(amount),
        counterparty=counterparty,
        sleeve="savings",
    )


def test_execution_facts_are_receipt_derived():
    expected = "executed 20.00 USD to Rail Stock Sleeve"
    assert execution_facts(_executed_receipt()) == expected


def test_matching_amount_passes():
    a = verify_execution_narration(
        "Bought $20.00 worth of Google shares.", _executed_receipt()
    )
    assert a.ok is True
    assert a.found_amount is True


def test_integer_rendering_passes():
    a = verify_execution_narration("Done, moved $20.", _executed_receipt())
    assert a.ok is True


def test_hallucinated_amount_is_flagged():
    a = verify_execution_narration("Done, moved $50.00.", _executed_receipt())
    assert a.ok is False
    assert a.reason == "amount_missing"


def test_non_executed_receipt_is_not_applicable():
    r = _executed_receipt().model_copy(update={"status": "rejected"})
    a = verify_execution_narration("Done!", r)
    assert a.applicable is False
    assert a.ok is True  # nothing to assert against
    assert execution_facts(r) == ""
