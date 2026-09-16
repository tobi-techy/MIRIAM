"""Miriam specification package."""

SPEC_VERSION = "1.2"
SPEC_PATH = "../docs/miriam_spec/miriam_spec_v1.2.md"

# Core registry exports
from miriam_agent.spec.registry import (
    SECTIONS,
    RULES,
    TRANSACTION_CLASSES,
    REASONING_ORDER,
    ANTI_PATTERNS,
    SPEC_HASH,
)

# Loading utilities
from miriam_agent.spec.registry import load_markdown, sections, content_hash

__all__ = [
    "SPEC_VERSION",
    "SPEC_PATH",
    "SECTIONS",
    "RULES",
    "TRANSACTION_CLASSES",
    "REASONING_ORDER",
    "ANTI_PATTERNS",
    "SPEC_HASH",
    "load_markdown",
    "sections",
    "content_hash",
]
