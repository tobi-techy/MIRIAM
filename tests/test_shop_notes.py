"""Unit tests for note parsing and the Jev-classified kinds. No network."""

from __future__ import annotations

from shop_fakes import FakeJudge

from miriam_agent.shop.notes import (
    classify_and_parse,
    parse_amount,
    parse_amounts,
    parse_due,
    parse_party,
)
from miriam_agent.shop.schemas import NoteKind


def test_plain_amount_forms() -> None:
    assert parse_amount("Owe 120k") == "120000"
    assert parse_amount("4,500") == "4500"
    assert parse_amount("N150,000") == "150000"
    assert parse_amount("15k") == "15000"
    assert parse_amount("no money here") is None


def test_amounts_never_render_in_exponent_form() -> None:
    assert parse_amounts("Owe 120k and 4,500") == ["120000", "4500"]
    assert parse_amounts("N150,000 15k") == ["150000", "15000"]
    for value in parse_amounts("1,200,000 and 120k and 15k"):
        assert "E" not in value


def test_amounts_ignores_a_crate_count_in_a_fraction() -> None:
    assert parse_amounts("bought 20 crates") == ["20"]
    assert parse_amount("2/3 of a crate") is None


def test_a_date_is_not_three_amounts() -> None:
    assert parse_amounts("due 17/10/2026") == []


def test_due_days() -> None:
    assert parse_due("Balance due Friday") == "friday"
    assert parse_due("pay tomorrow") == "tomorrow"
    assert parse_due("next week Monday") == "next week"
    assert parse_due("no day given") is None


def test_party_extraction() -> None:
    assert parse_party("Chinedu owes 12,500") == "Chinedu"
    assert parse_party("Alert 15k from Emeka") == "Emeka"
    assert parse_party("Paid Mama Nkechi 8k") == "Mama Nkechi"
    assert parse_party("Balance 30k due Friday") == ""


async def test_classify_keeps_the_amount_for_a_customer_debt() -> None:
    judge = FakeJudge(note_kind=NoteKind.CUSTOMER_DEBT.value)
    evaluation = await classify_and_parse("Chinedu owes 12,500", judge=judge)
    assert evaluation.kind is NoteKind.CUSTOMER_DEBT
    assert evaluation.parse.amount_naira == "12500"
    assert evaluation.needs_amount is False
    assert evaluation.unclassified is False
    assert evaluation.flags == []


async def test_supplier_delivery_number_is_a_quantity_not_naira() -> None:
    judge = FakeJudge(note_kind=NoteKind.SUPPLIER_DELIVERY.value)
    evaluation = await classify_and_parse("Bought 20 crates from Wigmore", judge=judge)
    assert evaluation.kind is NoteKind.SUPPLIER_DELIVERY
    assert evaluation.parse.amount_naira is None
    assert evaluation.parse.party == "Wigmore"
    assert evaluation.needs_amount is False


async def test_jev_unavailable_leaves_the_note_other_and_flagged() -> None:
    judge = FakeJudge(note_unavailable=True)
    evaluation = await classify_and_parse("Chinedu owes 12,500", judge=judge)
    assert evaluation.kind is NoteKind.OTHER
    assert evaluation.unclassified is True
    assert evaluation.flags == ["note not understood"]


async def test_money_note_without_an_amount_is_flagged_not_guessed() -> None:
    judge = FakeJudge(note_kind=NoteKind.PAYMENT_OUT.value)
    evaluation = await classify_and_parse("Paid the driver", judge=judge)
    assert evaluation.parse.amount_naira is None
    assert evaluation.needs_amount is True
    assert evaluation.flags == ["no amount found"]


async def test_empty_note_line_is_skipped() -> None:
    evaluation = await classify_and_parse("   ", judge=FakeJudge())
    assert evaluation.kind is NoteKind.OTHER
    assert evaluation.unclassified is False
    assert evaluation.flags == []


async def test_note_kind_not_in_the_catalog_is_treated_as_unclassified() -> None:
    judge = FakeJudge(note_kind="something_new")
    evaluation = await classify_and_parse("Random line", judge=judge)
    assert evaluation.kind is NoteKind.OTHER
    assert evaluation.unclassified is True
