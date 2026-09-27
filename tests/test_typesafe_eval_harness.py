"""Fast checks for the TypeSafe evaluation harness itself."""

from __future__ import annotations


def test_eval_harness_covers_money_catalog_and_decisions():
    from eval.typesafe_deep_eval import CATALOGS, MONEY_CASES

    assert "money" in CATALOGS
    assert {case["expected_branch"] for case in MONEY_CASES} == {
        "act:allow",
        "ask:allow_smaller",
        "ask:deny",
        "ask:none",
    }


def test_choice_margin_reads_probability_distribution():
    from typesafe_sdk import ChoiceAnswer

    from miriam_agent.judgment.answers import choice_margin

    answer = ChoiceAnswer.model_construct(
        choice="perform_task",
        confidence=0.9,
        probabilities={"perform_task": 0.55, "lookup_data": 0.45},
    )

    assert round(choice_margin(answer), 2) == 0.1
