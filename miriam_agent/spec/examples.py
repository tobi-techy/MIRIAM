"""Parse and validate spec examples from the markdown document.

This module extracts example blocks from the spec markdown document and provides
validation utilities for the good/bad example pairs.
"""

import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class SpecExample:
    """A single spec example with good and bad response pairs."""
    id: str
    section: str
    scenario: str
    bad: str
    good: str
    why_good: str
    failure_mode_prevented: str
    violated_rule: str  # Rule ID that the bad response violates

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpecExample":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def parse_markdown_example(self, content: str) -> "SpecExample":
        """Parse a markdown example block and return a SpecExample object."""
        # Extract ID from header
        id_match = re.search(r"## Example (\w+)", content)
        if not id_match:
            raise ValueError("Example ID not found")

        # Extract section from content
        section_match = re.search(r"Section: (\d+)", content)
        if not section_match:
            raise ValueError("Section not found")

        # Extract scenario
        scenario_match = re.search(r"Scenario: (.*?)\n\n", content, re.DOTALL)
        if not scenario_match:
            raise ValueError("Scenario not found")

        # Extract bad response
        bad_match = re.search(r"Bad Response:\s*\"(.*?)\"", content, re.DOTALL)
        if not bad_match:
            raise ValueError("Bad response not found")

        # Extract good response
        good_match = re.search(r"Good Response:\s*\"(.*?)\"", content, re.DOTALL)
        if not good_match:
            raise ValueError("Good response not found")

        # Extract why good
        why_good_match = re.search(r"Why Good:\s*\"(.*?)\"", content, re.DOTALL)
        if not why_good_match:
            raise ValueError("Why good not found")

        # Extract failure mode prevented
        failure_match = re.search(
            r"Failure Mode Prevented:\s*\"(.*?)\"", content, re.DOTALL
        )
        if not failure_match:
            raise ValueError("Failure mode prevented not found")

        # Extract violated rule
        violated_match = re.search(r"Violated Rule:\s*(\w+)", content)
        if not violated_match:
            raise ValueError("Violated rule not found")

        return SpecExample(
            id=id_match.group(1),
            section=section_match.group(1),
            scenario=scenario_match.group(1).strip(),
            bad=bad_match.group(1).strip(),
            good=good_match.group(1).strip(),
            why_good=why_good_match.group(1).strip(),
            failure_mode_prevented=failure_match.group(1).strip(),
            violated_rule=violated_match.group(1),
        )

    def validate_example_structure(self) -> list[str]:
        """Validate that the example has all required fields."""
        errors = []

        if not self.id:
            errors.append("Missing example ID")

        if not self.section:
            errors.append("Missing section")

        if not self.scenario:
            errors.append("Missing scenario")

        if not self.bad:
            errors.append("Missing bad response")

        if not self.good:
            errors.append("Missing good response")

        if not self.why_good:
            errors.append("Missing why good explanation")

        if not self.failure_mode_prevented:
            errors.append("Missing failure mode prevented")

        if not self.violated_rule:
            errors.append("Missing violated rule")

        return errors

    def validate_responses(self, quality_evaluator) -> list[str]:
        """Validate that the good response passes and bad response fails."""
        errors = []

        # Validate good response
        good_violations = quality_evaluator.evaluate_reply(
            self.good, self.violated_rule
        )
        if good_violations:
            errors.append(f"Good response should not violate rules: {good_violations}")

        # Validate bad response
        bad_violations = quality_evaluator.evaluate_reply(self.bad, self.violated_rule)
        if not bad_violations:
            errors.append(f"Bad response should violate rule {self.violated_rule}")

        return errors


def parse_spec_examples(markdown_content: str) -> list[SpecExample]:
    """Parse all examples from spec markdown content."""
    examples = []

    # Split content into example blocks
    example_blocks = re.split(r"## Example \w+", markdown_content)[1:]

    for block in example_blocks:
        try:
            example = SpecExample().parse_markdown_example(block)
            examples.append(example)
        except ValueError as e:
            print(f"Warning: Failed to parse example block: {e}")

    return examples


def lint_examples(
    spec_examples: list[SpecExample], quality_evaluator
) -> dict[str, Any]:
    """Lint all spec examples and return results."""
    results = {
        "total_examples": len(spec_examples),
        "valid_structure": 0,
        "valid_responses": 0,
        "invalid_examples": [],
        "example_results": [],
    }

    for example in spec_examples:
        example_result = {
            "id": example.id,
            "valid_structure": False,
            "structure_errors": [],
            "valid_responses": False,
            "response_errors": [],
            "overall_valid": False,
        }

        # Validate structure
        structure_errors = example.validate_example_structure()
        if not structure_errors:
            example_result["valid_structure"] = True
            results["valid_structure"] += 1
        else:
            example_result["structure_errors"] = structure_errors

        # Validate responses
        response_errors = example.validate_responses(quality_evaluator)
        if not response_errors:
            example_result["valid_responses"] = True
            results["valid_responses"] += 1
        else:
            example_result["response_errors"] = response_errors

        # Overall validity
        overall_valid = (
            example_result["valid_structure"] and example_result["valid_responses"]
        )
        example_result["overall_valid"] = overall_valid

        if not overall_valid:
            results["invalid_examples"].append(example_result)

        results["example_results"].append(example_result)

    return results


def get_example_by_id(
    spec_examples: list[SpecExample], example_id: str
) -> SpecExample | None:
    """Get an example by its ID."""
    for example in spec_examples:
        if example.id == example_id:
            return example
    return None


def get_examples_by_section(
    spec_examples: list[SpecExample], section: str
) -> list[SpecExample]:
    """Get all examples for a specific section."""
    return [example for example in spec_examples if example.section == section]


def get_examples_by_rule(
    spec_examples: list[SpecExample], rule: str
) -> list[SpecExample]:
    """Get all examples that test a specific rule."""
    return [example for example in spec_examples if example.violated_rule == rule]
