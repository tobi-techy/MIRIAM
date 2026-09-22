"""Processor registry (Stage 3): one entry point per document type.

Adding a type means writing a new module and registering it here — no
edits to the pipeline or unrelated processors.
"""

from __future__ import annotations

from collections.abc import Callable

from miriam_agent.documents.models import (
    DocumentKind,
    ExtractedText,
    StatementExtraction,
)
from miriam_agent.documents.processors import bank_statement

Handler = Callable[[ExtractedText], StatementExtraction | None]

_REGISTRY: dict[str, Handler] = {
    "bank_statement": bank_statement.extract_statement,
}


def get_processor(kind: DocumentKind) -> Handler | None:
    return _REGISTRY.get(kind)


def registered_types() -> list[str]:
    return sorted(_REGISTRY.keys())
