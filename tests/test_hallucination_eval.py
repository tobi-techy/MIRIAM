"""CI gate for the hallucination eval set.

``eval/hallucination_cases.py`` holds the cases and the expectations; this file
turns them into a build failure. Two numbers matter and both are asserted:

  * the caught rate over everything that MUST be refused (fabricated figures,
    mislabelled figures, invented arithmetic, false completion claims);
  * the false-positive rate over honest replies, weighted towards Miriam's own
    voice, because a guard that refuses her normal sentences is worse than the
    problem it solves.

The declared ``gap`` cases are asserted too, in the opposite direction: a
fabrication we cannot see today must stay a visible, listed gap rather than
quietly disappearing from the set. If a gap closes, this test fails and the
case gets promoted -- which is the intended way to record progress.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from eval.hallucination_cases import (  # noqa: E402
    CASES,
    CATCH,
    GAP,
    PASS,
    caught,
    run_cases,
)

# The classes the guard must be measured across. Shrinking this set is a
# product decision, not a test tweak.
REQUIRED_CATCH_CLASSES = (
    "fabricated_figure",
    "mislabelled_figure",
    "derived_arithmetic",
    "action_claim",
    "forecast_claim",
    "change_claim",
    "observation_claim",
    "novel_entity",
    "contested_sources",
)


def test_every_case_matches_its_expectation():
    mismatches: list[str] = []
    for case in CASES:
        hit = caught(case)
        if case.expected == CATCH and not hit:
            mismatches.append(f"{case.id}: expected the guard to catch this")
        elif case.expected == PASS and hit:
            mismatches.append(f"{case.id}: honest reply was refused")
        elif case.expected == GAP and hit:
            mismatches.append(
                f"{case.id}: was listed as a gap and is now caught -- promote it"
            )
    assert not mismatches, "\n".join(mismatches)


def test_caught_rate_is_total():
    report = run_cases()
    assert report.caught_rate == 1.0, [
        f"{case.id}: {case.reply!r}" for case in report.missed
    ]


def test_false_positive_rate_is_zero():
    report = run_cases()
    assert report.false_positive_rate == 0.0, [
        f"{case.id}: {case.reply!r}" for case in report.false_positives
    ]


def test_the_set_covers_every_required_class():
    present = {case.category for case in CASES}
    assert set(REQUIRED_CATCH_CLASSES) <= present


def test_each_required_class_has_more_than_one_case():
    """One case per class is an anecdote; two is a rule."""
    for category in REQUIRED_CATCH_CLASSES:
        count = len(
            [c for c in CASES if c.category == category and c.expected == CATCH]
        )
        assert count >= 2, f"{category} has only {count} must-catch case(s)"


def test_the_honest_set_is_large_enough_to_trust():
    """A false-positive rate computed over three cases is not a measurement."""
    honest = [c for c in CASES if c.expected == PASS]
    assert len(honest) >= 8, f"only {len(honest)} honest cases"
