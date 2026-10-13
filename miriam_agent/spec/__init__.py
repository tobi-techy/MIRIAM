"""Miriam specification package.

Kept and owned: a machine-checked spec is the point. See "Spec ownership" in
CONTRIBUTION.md for the decision and the delete/restore history.
"""

# Core registry exports
# Loading utilities
from miriam_agent.spec.registry import (
    ANTI_PATTERNS,
    REASONING_ORDER,
    RULES,
    SECTIONS,
    SPEC_HASH,
    SPEC_PATH,
    TRANSACTION_CLASSES,
    content_hash,
    load_markdown,
    sections,
)

SPEC_VERSION = "1.2"

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
