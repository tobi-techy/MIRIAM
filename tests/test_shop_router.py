"""Router tests: tier choice and escalation, with Jev faked. No network."""

from __future__ import annotations

from types import SimpleNamespace

from shop_fakes import FakeJudge

from miriam_agent.shop.router import (
    decide_escalation,
    decide_starting_tier,
    guess_doc_kind,
    same_cells_agree,
)


def _facts(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "file_type": "image/png",
        "width": 1408,
        "height": 768,
        "blur": 120.0,
        "brightness": 0.5,
        "page_count": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_easy_and_confident_starts_small() -> None:
    decision = await decide_starting_tier(
        _facts(), judge=FakeJudge(difficulty="easy", confidence=0.93)
    )
    assert decision.tier == "small"
    assert decision.doc_kind == "stock_book"
    assert decision.degraded is False


async def test_hard_starts_big() -> None:
    decision = await decide_starting_tier(
        _facts(), judge=FakeJudge(difficulty="hard", confidence=0.95)
    )
    assert decision.tier == "big"
    assert decision.difficulty == "hard"


async def test_low_confidence_starts_big_even_when_easy() -> None:
    decision = await decide_starting_tier(
        _facts(), judge=FakeJudge(difficulty="easy", confidence=0.55)
    )
    assert decision.tier == "big"


async def test_jev_outage_starts_big_and_keeps_go_hint() -> None:
    judge = FakeJudge(tier_unavailable=True)
    decision = await decide_starting_tier(_facts(), doc_hint="invoice", judge=judge)
    assert decision.tier == "big"
    assert decision.doc_kind == "invoice"
    assert decision.degraded is True


async def test_router_disabled_uses_the_deterministic_guess() -> None:
    decision = await decide_starting_tier(
        _facts(),
        caption="stock book",
        judge=FakeJudge(),
        router_enabled=False,
    )
    assert decision.tier == "big"
    assert decision.degraded is True
    assert decision.doc_kind == "stock_book"


async def test_empty_jev_choice_falls_back_to_the_go_hint() -> None:
    judge = FakeJudge(doc_kind="", confidence=0.9)
    judge.doc_kind = ""
    decision = await decide_starting_tier(
        _facts(), doc_hint="delivery_note", judge=judge
    )
    assert decision.doc_kind == "delivery_note"


def test_guess_doc_kind_reads_invoice_words() -> None:
    assert guess_doc_kind("INVOICE\nSubtotal\nPayment terms") == "invoice"
    assert guess_doc_kind("OPENING SUPPLIED SOLD LEFT") == "stock_book"
    assert guess_doc_kind("DELIVERY NOTE DN-2610-0388") == "delivery_note"
    assert guess_doc_kind("nothing familiar here") == "other"


async def test_two_failing_rows_escalate_without_asking_jev() -> None:
    judge = FakeJudge(quality=5)
    decision = await decide_escalation({}, [], rows_failing=2, judge=judge)
    assert decision.escalate is True
    assert judge.quality_calls == 0
    assert "2 rows fail the stock check" in decision.reason


async def test_low_quality_escalates() -> None:
    decision = await decide_escalation({"rows": []}, [], judge=FakeJudge(quality=2))
    assert decision.escalate is True
    assert decision.quality == 2
    assert "quality 2" in decision.reason


async def test_clean_read_does_not_escalate() -> None:
    decision = await decide_escalation(
        {"rows": []}, [], judge=FakeJudge(quality=5, misalignment=0.0)
    )
    assert decision.escalate is False
    assert decision.reason == ""


async def test_likely_misalignment_escalates() -> None:
    decision = await decide_escalation(
        {"rows": []}, [], judge=FakeJudge(quality=5, misalignment=0.8)
    )
    assert decision.escalate is True
    assert "misalignment 0.80" in decision.reason


async def test_quality_judge_outage_escalates() -> None:
    decision = await decide_escalation(
        {}, [], judge=FakeJudge(quality_unavailable=True)
    )
    assert decision.escalate is True
    assert decision.degraded is True
    assert decision.reason == "jev unavailable"


async def test_same_cells_agree_keeps_agreement_and_flags_disagreement() -> None:
    first = {"sold": 12, "left": 3, "price_naira": "1200"}
    second = {"sold": 12, "left": 4, "price_naira": "1200"}
    agreed, disagreements = await same_cells_agree(first, second)
    assert agreed == {"sold": 12, "price_naira": "1200"}
    assert disagreements == ["left"]
