"""Miriam specification package."""

# Core registry exports
# Loading utilities
from miriam_agent.spec.registry import (
    ANTI_PATTERNS,
    REASONING_ORDER,
    RULES,
    SECTIONS,
    SPEC_HASH,
    TRANSACTION_CLASSES,
    content_hash,
    load_markdown,
    sections,
)

SPEC_VERSION = "1.2"
SPEC_PATH = "../docs/miriam_spec/miriam_spec_v1.2.md"

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
