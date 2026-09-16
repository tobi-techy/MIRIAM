"""Tests for the ranked hypothesis engine (spec v1.2 §12).

The engine sniffs the joined interview text against structural causes and
returns them ranked with evidence. Determinism matters: the same words must
always rank the same way, and the engine can never invent a cause the user
never hinted at.
"""

from __future__ import annotations

from miriam_agent.financial.hypotheses import (
    PROBE_CATEGORIES,
    leak_probe_open,
    probe_categories_open,
    rank_hypotheses,
    top_hypotheses,
    top_hypothesis,
)


def _codes(text, limit=12):
    return [h.code for h in top_hypotheses(text, limit=limit)]


def test_ranks_lack_of_visibility_from_disappearing_money():
    ranked = rank_hypotheses(
        "not being able to keep money in check, it's as if money just "
        "disappears and I have no idea where it went"
    )
    assert ranked[0].code == "lack_of_visibility"
    assert ranked[0].confidence >= 0.5
    assert ranked[0].evidence, "a read with no evidence would be invented"
    assert any("disappear" in snippet for snippet in ranked[0].evidence)


def test_ranks_broke_before_payday_from_runway_phrases():
    top = top_hypothesis(
        "the month always ends before the money does, paycheck to paycheck"
    )
    assert top is not None
    assert top.code == "broke_before_payday"


def test_ranks_family_obligations_from_people_who_lean_on_them():
    top = top_hypothesis("I send money home to my mum and help my siblings")
    assert top is not None
    assert top.code == "family_obligations"


def test_ranks_irregular_income_from_lumpy_commission():
    top = top_hypothesis("I'm on commission and my income is all over the place")
    assert top is not None
    assert top.code == "irregular_income"


def test_no_hypothesis_without_evidence():
    assert rank_hypotheses("") == []
    assert top_hypothesis("yes") is None


def test_empty_text_is_handled():
    assert _codes("") == []
    assert _codes("   ") == []


def test_ranking_is_deterministic():
    text = "I overspend on random stuff and then the month runs out before payday"
    first = [h.code for h in rank_hypotheses(text)]
    for _ in range(3):
        assert [h.code for h in rank_hypotheses(text)] == first
        assert [h.evidence for h in rank_hypotheses(text)] == [
            h.evidence for h in rank_hypotheses(text)
        ]


def test_leak_probe_opens_when_channels_are_unresolved():
    # Only a weak control statement: no single leak channel is confident, so
    # Miriam should offer the concrete categories in one question.
    assert leak_probe_open(
        "something always comes up and the money keeps getting away from me"
    )


def test_leak_probe_closes_when_a_channel_resolves():
    # The user names the leak outright -- asking the category question again
    # would waste the turn.
    assert not leak_probe_open(
        "I overspend on random things, definitely overspending, too much "
        "on clothes and food every week"
    )


def test_probe_categories_are_the_canonical_four():
    labels = [label for label, _ in PROBE_CATEGORIES]
    assert labels == [
        "spending too much",
        "unexpected expenses",
        "helping other people",
        "not really knowing where the money went",
    ]


def test_open_categories_drop_resolved_channels():
    open_categories = probe_categories_open(
        "I overspend too much, definitely overspending, way too much on "
        "clothes and food"
    )
    assert "spending too much" not in open_categories
    assert "unexpected expenses" in open_categories


def test_evidence_is_trimmed_to_a_sane_length():
    ranked = rank_hypotheses(
        "I keep trying to budget but the budget never sticks and I start "
        "over every single month with no system at all whatsoever"
    )
    assert ranked[0].code == "lack_of_allocation_system"
    for snippet in ranked[0].evidence:
        assert len(snippet) <= 48
