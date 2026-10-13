"""Layer 2 - JUDGMENT. Jev questions for shop document reading.

The same rules as the money catalog: text only, typed questions, typed answers,
no prose back, and a transport failure is never a green light. This module is
the *only* place the shop reader talks to Jev.

Three catalogs live here:

* ``tier``   -- before a read: what kind of document is this, and is it easy or
  hard? Decides whether the read starts on the small or the big model.
* ``quality``-- after a read: how good is the extraction, and are numbers likely
  attached to the wrong item rows? Decides whether to escalate.
* ``note``   -- classify each raw margin note so plain parsers know what to do
  with its amounts, without the model writing any text or inventing a figure.

This module deliberately does not import the API, orchestrator, onboarding or
proactive layers (enforced by tests/architecture/test_module_rules.py).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    RetryPolicy,
    Score,
    ScoreAnswer,
    SystemOneResponse,
)

from miriam_agent.config.settings import get_settings
from miriam_agent.judgment.client import enabled
from miriam_agent.judgment.questions import Catalog
from miriam_agent.judgment.service import JudgmentUnavailableError, evaluate

logger = logging.getLogger(__name__)

TIER_CATALOG_VERSION = "1"
QUALITY_CATALOG_VERSION = "1"
NOTE_CATALOG_VERSION = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TierState(_Strict):
    """Cheap image facts the tier question is allowed to read."""

    caption: str = ""
    native_text: str = ""
    file_type: str = ""
    width: int = 0
    height: int = 0
    blur: float = 0.0
    brightness: float = 0.0
    page_count: int = 1


class QualityState(_Strict):
    """The extraction plus the deterministic check results, nothing else."""

    extraction: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    unclear_cells: list[str] = Field(default_factory=list)
    rows_failing: int = 0


class NoteState(_Strict):
    line: str = ""
    context: str = ""


class TierChoiceJudgment(SystemOneResponse):
    """The starting tier for one document read."""

    doc_kind: ChoiceAnswer
    difficulty: ChoiceAnswer


class ExtractionQualityJudgment(SystemOneResponse):
    """How clean a read is, and whether rows are likely misaligned."""

    extraction_quality: ScoreAnswer
    misalignment_likely: NoulAnswer


class NoteKindJudgment(SystemOneResponse):
    """The kind of one raw note line. Code parses the amounts."""

    kind: ChoiceAnswer


_DOC_KIND_CRITERIA = {
    "stock_book": {
        "what": "A ruled shop stock table: item rows with opening, supplied, "
        "sold, left and price columns, often handwritten.",
        "examples": ["handwritten exercise book with stock columns"],
    },
    "invoice": {
        "what": "A supplier's printed or written invoice with a header, item, "
        "quantity and amount columns, and a total to pay.",
        "examples": ["Wigmore Trading invoice", "distributor bill"],
    },
    "delivery_note": {
        "what": "A supplier's delivery or waybill listing goods delivered, "
        "with item and quantity, usually no prices or a running total.",
        "examples": ["delivery note DN-2610-0388", "waybill"],
    },
    "receipt": {
        "what": "A shop receipt for a purchase the owner made or took.",
        "examples": ["fuel receipt", "POS printout"],
    },
    "bank_statement": {
        "what": "A bank account statement with a date, description, debit, "
        "credit and balance columns.",
        "examples": ["GTBank statement page"],
    },
    "other": {
        "what": "Anything that is not one of the above, including a photo of "
        "shelves, a person, or an unreadable page.",
        "examples": ["shelf photo", "blank page"],
    },
}

_DIFFICULTY_CRITERIA = {
    "easy": {
        "what": "A clean, typed or clearly printed document: a native-text PDF, "
        "or a sharp, top-down photo with the whole table in view.",
        "examples": ["typed invoice PDF", "sharp well-lit page"],
    },
    "hard": {
        "what": "The page is dim, blurry, angled, partial, or handwritten, or "
        "it has several pages, or rows are crowded.",
        "examples": ["dim photo of handwriting", "multi-page exercise book"],
    },
}

_QUALITY_CRITERIA = [
    "Likely misread: digits do not resemble the image, or the table is guessed",
    "Weak: several rows uncertain, arithmetic does not add up",
    "Fair: readable but some cells unclear or crossed out",
    "Good: mostly clean, at most one low-confidence cell",
    "Clean and consistent: every row reads plainly and the arithmetic holds",
]

_NOTE_KIND_CRITERIA = {
    "customer_debt": {
        "what": "A named person owes the shop money already owed.",
        "examples": ["Mrs Bisi owes 3,200", "Iya Basira 4,500"],
    },
    "payment_out": {
        "what": "Money the shop paid out to a supplier or person.",
        "examples": ["paid Wigmore 150k transfer"],
    },
    "payment_in_unverified": {
        "what": "A claimed incoming payment that is not yet verified against "
        "the bank.",
        "examples": ["alert 15k from Emeka", "Emeka said he sent 15k"],
    },
    "supplier_delivery": {
        "what": "Goods a supplier brought, with no invoice or receipt yet.",
        "examples": ["Coke man brought 24, no receipt"],
    },
    "price_change": {
        "what": "A note that a price has changed.",
        "examples": ["price don go up", "new price 500"],
    },
    "other": {
        "what": "Anything else, including a comment with no money meaning.",
        "examples": ["reorder next week"],
    },
}


TIER_QUESTIONS: dict[str, Any] = {
    "doc_kind": Choice(
        instructions=(
            "Using `caption` and `native_text` (and the image facts as weak "
            "hints), what kind of document is this? If the text cannot be "
            "established, answer other rather than guessing."
        ),
        criteria=_DOC_KIND_CRITERIA,
    ),
    "difficulty": Choice(
        instructions=(
            "Given the image facts, `native_text` and `caption`, how hard is "
            "this page to read accurately?"
        ),
        criteria=_DIFFICULTY_CRITERIA,
    ),
}

QUALITY_QUESTIONS: dict[str, Any] = {
    "extraction_quality": Score(
        instructions=(
            "Judge `extraction` against `checks`, `rows_failing` and "
            "`unclear_cells`. How clean and internally consistent is the "
            "extraction?"
        ),
        criteria=_QUALITY_CRITERIA,
    ),
    "misalignment_likely": Noul(
        instructions=(
            "In `extraction`, are numbers likely attached to the wrong item "
            "rows (for example, values that belong a row up or down)?"
        ),
    ),
}

NOTE_QUESTIONS: dict[str, Any] = {
    "kind": Choice(
        instructions=(
            "Classify the single note line `line` using `context`. Answer with "
            "the kind only; code extracts any amounts."
        ),
        criteria=_NOTE_KIND_CRITERIA,
    ),
}


TIER = Catalog(
    name="shop_tier",
    version=TIER_CATALOG_VERSION,
    questions=TIER_QUESTIONS,
    response_model=TierChoiceJudgment,
)

QUALITY = Catalog(
    name="shop_quality",
    version=QUALITY_CATALOG_VERSION,
    questions=QUALITY_QUESTIONS,
    response_model=ExtractionQualityJudgment,
)

NOTE = Catalog(
    name="shop_note",
    version=NOTE_CATALOG_VERSION,
    questions=NOTE_QUESTIONS,
    response_model=NoteKindJudgment,
)


_reader_client: AsyncTypeSafeClient | None = None


def get_reader_client() -> AsyncTypeSafeClient:
    """A dedicated Jev client for the reader: one attempt, short timeout.

    Separate from the money client on purpose: a read must not queue behind
    three retries. A transport failure is raised so the caller can escalate.
    """
    global _reader_client
    if _reader_client is None:
        settings = get_settings()
        _reader_client = AsyncTypeSafeClient(
            api_key=settings.TYPESAFE_API_KEY,
            model=settings.TYPESAFE_MODEL,
            timeout=settings.SHOP_READ_JEV_TIMEOUT_S,
            retry=RetryPolicy(
                max_retries=0,
                respect_retry_after=False,
                timeout=settings.SHOP_READ_JEV_TIMEOUT_S,
            ),
        )
    return _reader_client


async def close_reader_client() -> None:
    """Close the reader client during application shutdown."""
    global _reader_client
    client = _reader_client
    _reader_client = None
    if client is not None:
        await client.aclose()


def _require_enabled() -> None:
    if not enabled():
        raise JudgmentUnavailableError("shop_tier", "JEV is disabled or has no key")


class ReadingJudge:
    """The three Jev calls the reader makes, each one attempt.

    Any transport or configuration failure raises
    :class:`~miriam_agent.judgment.service.JudgmentUnavailableError`; the caller
    fails closed (start on the big model, or ask the owner).
    """

    async def choose_tier(self, state: TierState) -> TierChoiceJudgment:
        _require_enabled()
        response = await evaluate(state, TIER, client=get_reader_client())
        return response  # type: ignore[return-value]

    async def check_quality(self, state: QualityState) -> ExtractionQualityJudgment:
        _require_enabled()
        response = await evaluate(state, QUALITY, client=get_reader_client())
        return response  # type: ignore[return-value]

    async def classify_note(self, state: NoteState) -> NoteKindJudgment:
        _require_enabled()
        response = await evaluate(state, NOTE, client=get_reader_client())
        return response  # type: ignore[return-value]


__all__ = [
    "NOTE",
    "QUALITY",
    "TIER",
    "ExtractionQualityJudgment",
    "NoteKindJudgment",
    "NoteState",
    "QualityState",
    "ReadingJudge",
    "TierChoiceJudgment",
    "TierState",
    "close_reader_client",
    "get_reader_client",
]
