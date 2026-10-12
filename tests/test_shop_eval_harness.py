"""Tests for the eval harness itself, with no network and no real model.

The harness is what turns a live read into a reported verdict, so its own
grading has to be right: a miss must be a miss, a dropped mode must be dropped
for the stated reason, and the figures it compares must come from the saved
extraction rather than the reply text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from shop_fakes import mama_tobi_book, wigmore_invoice

from eval.shop_read_eval import (
    CASES,
    Observation,
    _observe,
    _summary,
    _table,
    _usable_modes,
)


def case(name: str):
    return next(c for c in CASES if c.name == name)


def test_routed_is_dropped_but_small_and_big_survive_without_a_jev_key() -> None:
    modes, notes = _usable_modes(["small", "big", "routed"], has_jev_key=False)
    assert modes == ["small", "big"]
    assert len(notes) == 1
    assert "TYPESAFE_API_KEY" in notes[0]


def test_every_mode_survives_with_a_jev_key() -> None:
    modes, notes = _usable_modes(["small", "big", "routed"], has_jev_key=True)
    assert modes == ["small", "big", "routed"]
    assert notes == []


def test_routed_alone_is_left_with_no_run_to_do() -> None:
    modes, notes = _usable_modes(["routed"], has_jev_key=False)
    assert modes == []
    assert len(notes) == 1


def test_stock_book_figures_come_from_the_saved_extraction() -> None:
    saved = {
        "doc_type": "stock_book",
        "raw_extraction": mama_tobi_book().model_dump(),
    }
    values = _observe(SimpleNamespace(doc_type="stock_book"), saved)
    assert values["computed_total"] == "₦286,300"
    assert values["written_total"] == "₦254,400"
    assert values["gap"] == "₦31,900"
    assert values["rows_failing"] == 0
    assert values["rows_read"] == 8


def test_invoice_figures_name_the_one_bad_line() -> None:
    saved = {
        "doc_type": "invoice",
        "raw_extraction": wigmore_invoice().model_dump(),
    }
    values = _observe(SimpleNamespace(doc_type="invoice"), saved)
    assert values["computed_subtotal"] == "₦180,600"
    assert values["printed_subtotal"] == "₦182,600"
    assert values["subtotal_delta"] == "₦2,000"
    assert values["line_mismatches"] == 1
    assert values["line_mismatch_1"].startswith("Milo 400g refill: 6 x ₦3,800")
    assert values["total"] == "₦180,000"
    assert values["paid"] == "₦150,000"
    assert values["balance"] == "₦30,000"
    assert values["due"] == "17/10/2026"


def test_a_page_that_was_not_read_reports_only_its_kind() -> None:
    values = _observe(SimpleNamespace(doc_type="receipt"), {})
    assert values == {"doc_type": "receipt"}


def test_observation_without_misses_is_a_pass() -> None:
    observation = Observation(ok=True, values={"gap": "₦31,900"})
    assert observation.misses == []
    assert observation.error == ""


def test_table_and_summary_report_a_miss_rather_than_hiding_it() -> None:
    hit = Observation(ok=True, values={"gap": "₦31,900"}, latency_s=4.0, cost_usd=0.01)
    miss = Observation(
        ok=False,
        values={"gap": "₦31,000"},
        misses=["gap: expected '₦31,900', got '₦31,000'"],
        latency_s=8.0,
        cost_usd=0.02,
    )
    rows = [
        (case("mama_tobi_stock_book"), "small", hit),
        (case("mama_tobi_stock_book"), "big", miss),
    ]
    table = _table(rows)
    assert "| mama_tobi_stock_book | small |" in table
    assert "| match |" in table
    assert "MISS (1)" in table
    summary = _summary(rows, ["small", "big"])
    assert "small: 1/1 exact" in summary
    assert "big: 0/1 exact" in summary


def test_every_case_expectation_has_a_fixture_path_under_tests() -> None:
    for entry in CASES:
        assert entry.filename in {
            "stock_book_mama_tobi.png",
            "stock_book_ledger_v2.png",
            "stock_book_ledger_v2_scan.pdf",
            "wigmore_invoice_WGT-2610-0417.pdf",
            "wigmore_invoice_phone_photo.jpg",
        }


@pytest.mark.parametrize(
    "name",
    [
        "mama_tobi_stock_book",
        "ledger_v2_stock_book",
        "ledger_v2_scanned_pdf",
        "wigmore_invoice_pdf",
        "wigmore_invoice_photo",
    ],
)
def test_expected_figures_are_all_in_naira_form_or_plain(name: str) -> None:
    """Every money expectation carries the one currency symbol, never a float."""
    money_keys = {
        "computed_total",
        "written_total",
        "gap",
        "computed_subtotal",
        "printed_subtotal",
        "subtotal_delta",
        "discount",
        "total",
        "paid",
        "balance",
    }
    for key, expected in case(name).expect.items():
        if key not in money_keys:
            continue
        assert isinstance(expected, str)
        assert expected.startswith("₦"), (name, key, expected)
        assert "E" not in expected, (name, key, expected)
