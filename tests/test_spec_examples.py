"""Tests for the spec examples in the Miriam behavioral specification.

This test suite validates that the examples in the spec markdown document
are correct and that both good and bad responses are properly classified.

The document is the source of truth for its own format; the parser in
``miriam_agent.spec.examples`` adapts to it. These tests assert against the
document's real content, and two of them are ratchets in the style of
``scripts/mypy_ratchet.py``: they record the true current state and fail if it
gets worse, instead of asserting a target the document does not meet yet.
"""

import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from miriam_agent.onboarding.quality import evaluate_reply
from miriam_agent.spec import load_markdown
from miriam_agent.spec.examples import SpecExample, parse_spec_examples

# The document's own acceptance criterion (its §8.2 area) asks for at least 20
# good/bad examples. It carries 5. This floor is the true current count and may
# only rise: it fails the day someone removes an example, and it is raised when
# examples are added. The gap to 20 is tracked in docs/PRODUCTION-READINESS.md
# ("Follow-ups worth opening").
SPEC_EXAMPLE_FLOOR = 5

# Two examples demonstrate failure modes the deterministic linter cannot score
# from the document alone, because the rule they break needs turn context the
# document does not print:
#
#   Example 1 (R11/R12) -- R11 is scored only against a previous user turn, and
#     quality.py's own R11 docstring cites this very reply ("okay. but what
#     does 'going broke' actually look like for you?") as a *compliant echo*;
#     R12 needs a therapist phrase and this tail is not one. The document and
#     the linter disagree about this reply.
#   Example 2 (R10) -- R10 is scored only against a grounding block, and the
#     example's point is that no statement was referenced. Given the statement's
#     balance as grounding, the bad response is caught but the *good* one trips
#     R10 too, for the $500 it recommends that the statement never mentions.
#
# This inventory is a ratchet: it may shrink, never grow. Adding to it hides a
# real regression, so `test_unscored_examples_are_the_named_gaps` checks both
# halves -- each listed example really is unscored, and it belongs to a rule
# that needs turn context.
EXAMPLES_THE_LINTER_CANNOT_SCORE = {"1", "2"}


def examples() -> list[SpecExample]:
    return parse_spec_examples(load_markdown())


def test_examples_are_parsable():
    """Test that spec examples can be parsed from markdown."""
    parsed = examples()

    assert isinstance(parsed, list), "Examples should be a list"
    assert parsed, "The document's examples section should yield examples"


def test_examples_have_required_fields():
    """Test that parsed examples have all required fields."""
    for example in examples():
        assert isinstance(
            example, SpecExample
        ), f"Example should be SpecExample: {example}"
        assert example.number, "Example should have a number"
        assert example.title, "Example should have a title"
        assert example.scenario, "Example should have a scenario"
        assert example.bad, "Example should have a bad response"
        assert example.good, "Example should have a good response"
        assert example.why_bad, "Example should have a why bad explanation"
        assert example.why_good, "Example should have why good explanation"
        assert example.violated_rule, "Example should name the rule it violates"
        assert example.rule_refs, f"{example.id} title should cite at least one rule"


def test_good_examples_pass_lint():
    """Test that good examples pass the quality lint."""
    for example in examples():
        violations = evaluate_reply(example.good)
        assert not violations, (
            f"Good example {example.id} should pass lint but has "
            f"violations: {violations}"
        )


def test_bad_examples_fail_lint():
    """Test that bad examples fail the quality lint on the rule they name."""
    for example in examples():
        violations = evaluate_reply(example.bad)
        scored = bool(violations) and example.violated_rule in str(violations)
        assert scored or example.number in EXAMPLES_THE_LINTER_CANNOT_SCORE, (
            f"Bad example {example.id} should violate "
            f"{example.violated_rule} but the linter reported {violations}; add "
            "it to EXAMPLES_THE_LINTER_CANNOT_SCORE only with a reason"
        )


def test_unscored_examples_are_the_named_gaps():
    """The unscorable inventory is a ratchet: it may shrink, never grow."""
    by_number = {example.number: example for example in examples()}
    missing = EXAMPLES_THE_LINTER_CANNOT_SCORE - set(by_number)
    assert not missing, f"Listed examples no longer exist in the document: {missing}"

    for number in sorted(EXAMPLES_THE_LINTER_CANNOT_SCORE):
        example = by_number[number]
        assert not evaluate_reply(example.bad), (
            f"{example.id} is now scored by the linter; remove it from "
            "EXAMPLES_THE_LINTER_CANNOT_SCORE"
        )
        assert example.violated_rule in {"R10", "R11"}, (
            f"{example.id} names {example.violated_rule}; only the rules that "
            "need turn context (R10 grounding, R11 previous turn) may be "
            "listed as unscorable"
        )


def test_spec_examples_have_minimum_count():
    """Test that the example count has not fallen below the recorded floor."""
    parsed = examples()

    assert len(parsed) >= SPEC_EXAMPLE_FLOOR, (
        f"Should have at least {SPEC_EXAMPLE_FLOOR} examples, found {len(parsed)}; "
        "the document's own target is 20+ (see docs/PRODUCTION-READINESS.md)"
    )


def test_example_ids_are_unique():
    """Test that all example IDs are unique."""
    numbers = [example.number for example in examples()]

    assert len(numbers) == len(
        set(numbers)
    ), f"Example numbers should be unique, found duplicates: {numbers}"


def test_examples_cover_different_rule_refs():
    """Test that examples cover different rules of the spec."""
    rules = {rule for example in examples() for rule in example.rule_refs}

    assert len(rules) >= 3, f"Examples should cover multiple rules, found: {rules}"


def test_examples_have_meaningful_violations():
    """Test that the scored bad examples violate the rule the document names."""
    scored = [
        example
        for example in examples()
        if example.number not in EXAMPLES_THE_LINTER_CANNOT_SCORE
    ]
    assert scored, "At least one bad example should be scorable by the linter"

    for example in scored:
        violations = evaluate_reply(example.bad)
        assert violations, f"Example {example.id} should violate at least one rule"
        assert example.violated_rule in violations, (
            f"Example {example.id} should violate {example.violated_rule}, "
            f"the linter reported {violations}"
        )


def test_spec_examples_integration_with_quality_module():
    """Test that spec examples integrate with the quality evaluation module."""
    for example in examples():
        good_violations = evaluate_reply(example.good)
        assert (
            not good_violations
        ), f"Example {example.id} good response should pass: {good_violations}"

        if example.number in EXAMPLES_THE_LINTER_CANNOT_SCORE:
            continue

        bad_violations = evaluate_reply(example.bad)
        assert bad_violations, f"Example {example.id} bad response should fail"


def test_examples_have_explanatory_text():
    """Test that examples have explanatory text for why they are good/bad."""
    for example in examples():
        assert (
            len(example.why_good) > 10
        ), f"Example {example.id} should have a meaningful why good explanation"
        assert (
            len(example.why_bad) > 10
        ), f"Example {example.id} should have a meaningful why bad explanation"


def test_parse_spec_examples_reports_a_malformed_block():
    """A block that does not match the format is reported, never dropped."""
    from miriam_agent.spec.examples import SpecExampleError

    with pytest.raises(SpecExampleError):
        parse_spec_examples(
            "## Specification Examples\n\n"
            "### Example 9: Broken\n"
            "**Bad:**\n"
            '"bad"\n'
        )


def test_spec_examples_can_be_loaded_without_markdown():
    """Test that spec examples can be built without the markdown document."""
    example = SpecExample(
        number="test-example",
        title="Test scenario (R1)",
        bad="Bad response",
        why_bad="Why it is bad",
        good="Good response",
        why_good="Why it's good",
    )

    assert example.number == "test-example"
    assert example.id == "Example test-example"
    assert example.scenario == "Test scenario"
    assert example.rule_refs == ["R1"]
    assert example.bad == "Bad response"
    assert example.good == "Good response"


def test_example_validation_handles_errors_gracefully():
    """Test that example validation reports missing fields instead of raising."""
    example = SpecExample(
        number="incomplete",
        title="",
        bad="",
        why_bad="",
        good="",
        why_good="",
    )

    errors = example.validate_example_structure()

    assert "Missing title" in errors
    assert "Missing good" in errors
    assert example.number == "incomplete"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
