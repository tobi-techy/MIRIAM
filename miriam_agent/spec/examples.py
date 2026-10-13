"""Parse and validate spec examples from the markdown document.

The document at ``docs/miriam_spec/miriam_spec_v1.2.md`` is the source of truth
for its own format: it is product content the owner authors, and this module
adapts to it rather than the other way round. Each example is a level-3 heading
under the document's ``## Specification Examples`` section::

    ### Example 1: Problem Identification (R1-R2)
    **Bad:**
    "the response the agent must not send"

    **Why Bad:** the failure mode it demonstrates, naming the rules it breaks

    **Good:**
    "the response that conforms"

    **Why Good:** why the good response works

A block that does not carry all four labels raises :class:`SpecExampleError`
naming the example. Nothing is dropped quietly: an example that failed to parse
would silently weaken every count and coverage assertion that follows.
"""

import re
from dataclasses import asdict, dataclass
from typing import Any

EXAMPLES_SECTION_HEADING = "## Specification Examples"

_EXAMPLES_SECTION_RE = re.compile(
    rf"(?ms)^{re.escape(EXAMPLES_SECTION_HEADING)}\s*$(.*?)(?=^## |\Z)"
)
_EXAMPLE_HEADING_RE = re.compile(r"(?m)^### Example (\d+):[ \t]*(.+?)[ \t]*$")
_LABEL_RE = re.compile(r"\*\*(Bad|Why Bad|Good|Why Good):\*\*")
_RULE_RE = re.compile(r"\bR\d+\b")


class SpecExampleError(ValueError):
    """An example block in the document does not match the documented format."""


@dataclass
class SpecExample:
    """One good/bad response pair from the spec document."""

    number: str  # "1", from "### Example 1: ..."
    title: str  # "Problem Identification (R1-R2)"
    bad: str
    why_bad: str
    good: str
    why_good: str

    @property
    def id(self) -> str:
        """The example's identifier, as the document writes it."""
        return f"Example {self.number}"

    @property
    def scenario(self) -> str:
        """The example's title without its trailing rule references."""
        return re.sub(r"\s*\([^)]*\)\s*$", "", self.title).strip()

    @property
    def rule_refs(self) -> list[str]:
        """The rule ids the title cites, e.g. ``["R1", "R2"]``."""
        return _RULE_RE.findall(self.title)

    @property
    def violated_rule(self) -> str | None:
        """The first rule the "Why Bad" text names, falling back to the title."""
        mentioned: list[str] = _RULE_RE.findall(self.why_bad)
        if mentioned:
            return mentioned[0]
        refs: list[str] = self.rule_refs
        return refs[0] if refs else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpecExample":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate_example_structure(self) -> list[str]:
        """The fields this example is missing, if any."""
        return [
            f"Missing {field}"
            for field in ("number", "title", "bad", "why_bad", "good", "why_good")
            if not getattr(self, field)
        ]


def _unquote(text: str) -> str:
    """Strip the quotes the document wraps a response in, keeping any note."""
    text = text.strip()
    if text.startswith('"'):
        text = text[1:]
    quoted = re.match(r'^(.*?)"\s*(?:\(.*\))?\s*$', text, re.DOTALL)
    if quoted:
        text = quoted.group(1)
    return text.strip()


def _parse_block(number: str, title: str, body: str) -> SpecExample:
    parts = _LABEL_RE.split(body)
    labels = parts[1::2]
    contents = parts[2::2]

    if sorted(labels) != ["Bad", "Good", "Why Bad", "Why Good"]:
        raise SpecExampleError(
            f"{EXAMPLES_SECTION_HEADING}, Example {number}: expected the labels "
            "**Bad:**, **Why Bad:**, **Good:** and **Why Good:**, found "
            f"{labels or 'none'}"
        )

    fields = dict(zip(labels, (content.strip() for content in contents), strict=True))
    empty = [label for label, value in fields.items() if not value]
    if empty:
        raise SpecExampleError(
            f"{EXAMPLES_SECTION_HEADING}, Example {number}: empty {empty}"
        )

    return SpecExample(
        number=number,
        title=title,
        bad=_unquote(fields["Bad"]),
        why_bad=" ".join(fields["Why Bad"].split()),
        good=_unquote(fields["Good"]),
        why_good=" ".join(fields["Why Good"].split()),
    )


def parse_spec_examples(markdown_content: str) -> list[SpecExample]:
    """Parse every example from the document's Specification Examples section.

    Raises :class:`SpecExampleError` if the section heading is missing or if any
    block under it does not carry the documented labels, so a malformed example
    can never be silently excluded from the counts the spec gate asserts.
    """
    section = _EXAMPLES_SECTION_RE.search(markdown_content)
    if not section:
        raise SpecExampleError(
            f"the spec document has no '{EXAMPLES_SECTION_HEADING}' section"
        )

    headings = list(_EXAMPLE_HEADING_RE.finditer(section.group(1)))
    examples = []
    for index, heading in enumerate(headings):
        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(section.group(1))
        )
        examples.append(
            _parse_block(
                heading.group(1),
                heading.group(2),
                section.group(1)[heading.end() : end],
            )
        )
    return examples


def get_example_by_id(
    spec_examples: list[SpecExample], example_id: str
) -> SpecExample | None:
    """Get an example by its number or by its full id ("Example 1")."""
    for example in spec_examples:
        if example.number == example_id or example.id == example_id:
            return example
    return None


def get_examples_by_rule_ref(
    spec_examples: list[SpecExample], rule: str
) -> list[SpecExample]:
    """Get every example whose title cites ``rule``."""
    return [example for example in spec_examples if rule in example.rule_refs]


def get_examples_by_rule(
    spec_examples: list[SpecExample], rule: str
) -> list[SpecExample]:
    """Get every example whose bad response is meant to violate ``rule``."""
    return [example for example in spec_examples if example.violated_rule == rule]
