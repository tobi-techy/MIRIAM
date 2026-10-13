"""Fakes for the shop reader tests: a Jev judge and a vision provider.

Both fakes are plain duck-typed objects. No test in this file or its importers
touches the network: the Jev judge returns the three catalog answers directly,
and the provider replays queued JSON payloads for the extract pass and a fixed
correction list for the verify pass.
"""

from __future__ import annotations

import json
from typing import Any

from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer

from miriam_agent.agents.llm import LLMResponse
from miriam_agent.core.exceptions import AgentError
from miriam_agent.judgment.reading import (
    ExtractionQualityJudgment,
    NoteKindJudgment,
    TierChoiceJudgment,
)
from miriam_agent.judgment.service import JudgmentUnavailableError
from miriam_agent.shop.schemas import (
    InvoiceLine,
    StockBook,
    StockRow,
    SupplierDocument,
)


def _choice(value: str, confidence: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer.model_construct(
        type="choice",
        choice=value,
        confidence=confidence,
        probabilities={value: confidence},
    )


def _score(value: int) -> ScoreAnswer:
    return ScoreAnswer.model_construct(
        type="score",
        score=value,
        confidence=0.9,
        legend={},
        probabilities={value: 0.9},
    )


def _noul(value: float) -> NoulAnswer:
    return NoulAnswer.model_construct(type="noul", noul=value)


class FakeJudge:
    """Stands in for :class:`ReadingJudge` with fixed answers."""

    def __init__(
        self,
        *,
        doc_kind: str = "stock_book",
        difficulty: str = "easy",
        confidence: float = 0.9,
        quality: int = 5,
        misalignment: float = 0.0,
        note_kind: str = "other",
        tier_unavailable: bool = False,
        quality_unavailable: bool = False,
        note_unavailable: bool = False,
    ) -> None:
        self.doc_kind = doc_kind
        self.difficulty = difficulty
        self.confidence = confidence
        self.quality = quality
        self.misalignment = misalignment
        self.note_kind = note_kind
        self.tier_unavailable = tier_unavailable
        self.quality_unavailable = quality_unavailable
        self.note_unavailable = note_unavailable
        self.tier_calls = 0
        self.quality_calls = 0
        self.note_calls = 0

    async def choose_tier(self, state: Any) -> TierChoiceJudgment:
        self.tier_calls += 1
        if self.tier_unavailable:
            raise JudgmentUnavailableError("shop_tier", "jev down")
        return TierChoiceJudgment.model_construct(
            model="fake",
            usage={},
            answers=[],
            doc_kind=_choice(self.doc_kind, self.confidence),
            difficulty=_choice(self.difficulty, self.confidence),
        )

    async def check_quality(self, state: Any) -> ExtractionQualityJudgment:
        self.quality_calls += 1
        if self.quality_unavailable:
            raise JudgmentUnavailableError("shop_quality", "jev down")
        return ExtractionQualityJudgment.model_construct(
            model="fake",
            usage={},
            answers=[],
            extraction_quality=_score(self.quality),
            misalignment_likely=_noul(self.misalignment),
        )

    async def classify_note(self, state: Any) -> NoteKindJudgment:
        self.note_calls += 1
        if self.note_unavailable:
            raise JudgmentUnavailableError("shop_note", "jev down")
        return NoteKindJudgment.model_construct(
            model="fake",
            usage={},
            answers=[],
            kind=_choice(self.note_kind, self.confidence),
        )


class FakeProvider:
    """Replays queued extraction payloads, then a fixed verify answer."""

    def __init__(
        self,
        payloads: list[Any] | None = None,
        *,
        verify: dict[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self.payloads = list(payloads or [])
        self.verify = verify if verify is not None else {"corrections": []}
        self.fail = fail
        self.extract_calls = 0
        self.verify_calls = 0
        self.schemas: list[str] = []
        self.images: list[list[str] | None] = []
        # The prompts, so a test can assert what the model was asked.
        self.messages: list[list[Any]] = []

    async def complete_structured(
        self,
        messages: list[Any],
        schema_model: Any,
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.schemas.append(schema_model.__name__)
        self.messages.append(list(messages))
        for message in messages:
            images = getattr(message, "images", None)
            if images:
                self.images.append(images)
        if self.fail:
            raise AgentError("provider down")
        if schema_model.__name__ == "Verification":
            self.verify_calls += 1
            return LLMResponse(content=json.dumps(self.verify), model="fake-verify")
        self.extract_calls += 1
        if not self.payloads:
            raise AgentError("no queued payload")
        payload = self.payloads.pop(0)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(content=content, model="fake-extract")


def provider_factory(small: FakeProvider, big: FakeProvider | None = None) -> Any:
    """A ``provider_factory(tier)`` backed by two fakes."""

    def _factory(tier: str) -> FakeProvider:
        return small if tier == "small" else (big or small)

    return _factory


def mama_tobi_book(**overrides: Any) -> StockBook:
    """The Mama Tobi page: rows add up, the written total is ₦31,900 short.

    Rows are internally consistent on purpose so the only failure in the checks
    is the written total, which is what the amendment expects this page to show.
    """
    sales = [
        ("Rice 50kg", 3, "12000"),
        ("Beans paint bucket", 8, "5500"),
        ("Peak milk tin", 5, "9000"),
        ("Indomie carton", 10, "3800"),
        ("Kings oil 1L", 7, "4500"),
        ("Milo 400g", 4, "7200"),
        ("Sugar 1kg", 6, "2500"),
        ("Detergent carton", 2, "24000"),
    ]
    rows = [
        StockRow(
            item=item,
            opening=sold,
            supplied=10,
            sold=sold,
            left=10,
            price_naira=price,
            confidence=0.95,
        )
        for item, sold, price in sales
    ]
    data: dict[str, Any] = {
        "shop": "Mama Tobi",
        "date": "11/10/2026",
        "rows": rows,
        "written_total_sales_naira": "254400",
        "notes": [],
    }
    data.update(overrides)
    return StockBook.model_validate(data)


def wigmore_invoice(**overrides: Any) -> SupplierDocument:
    """The Wigmore invoice, including the ₦2,000 Milo line mistake."""
    lines = [
        ("Indomie Instant Noodles 70g (ctn of 40)", 10, "9200", "92000"),
        ("Peak Evaporated Milk 160g tin", 24, "400", "9600"),
        ("Dangote Spaghetti 500g", 20, "950", "19000"),
        ("Milo 400g refill", 6, "3800", "24800"),
        ("Kings Vegetable Oil 1L", 12, "3100", "37200"),
    ]
    data: dict[str, Any] = {
        "doc_kind": "invoice",
        "supplier": "Wigmore Trading Ltd",
        "number": "WGT/INV/2610-0417",
        "date": "10/10/2026",
        "buyer": "Mama Tobi Stores",
        "lines": [
            InvoiceLine(
                item=item,
                qty=qty,
                unit_price_naira=price,
                line_total_naira=total,
                confidence=0.95,
            )
            for item, qty, price, total in lines
        ],
        "subtotal_naira": "182600",
        "discount_naira": "2600",
        "total_naira": "180000",
        "paid_naira": "150000",
        "balance_naira": "30000",
        "due_date": "17/10/2026",
        "payment_terms": "7 days credit",
        "notes": [],
    }
    data.update(overrides)
    return SupplierDocument.model_validate(data)


__all__ = [
    "FakeJudge",
    "FakeProvider",
    "mama_tobi_book",
    "provider_factory",
    "wigmore_invoice",
]


def ada_stock_book(**overrides: Any) -> StockBook:
    """The page off Ada's counter, read live from an iPhone photo.

    Two blocks on one page: October (left) has an opening balance column and
    every row adds up; November (right, 01/11-29/11) has no opening column at
    all, so its openings are null and the checks carry October's closing forward
    by code. The numbers are the ones the live read produced: the reader copied
    "quantity received" into opening and flagged all ten November rows, which is
    the failure this page now guards.
    """
    october = [
        ("Indomie (Curry)", 50, 20, 10, 60),
        ("Peak Milk (500g)", 30, 50, 20, 60),
        ("Golden Morn", 40, 30, 15, 55),
        ("Eva Water (75cl)", 100, 100, 40, 160),
        ("Milo (400g)", 20, 30, 10, 40),
        ("Soya Beans", 200, 50, 30, 220),
        ("Tomato Paste", 60, 40, 25, 75),
        ("Dan-Q Noodles", 35, 25, 15, 45),
        ("Peak Milk (1L)", 25, 40, 20, 45),
        ("Dettol (750ml)", 15, 20, 8, 27),
        ("Sunlight Soap", 50, 60, 30, 80),
        ("Lux Soap", 40, 50, 20, 70),
        ("Nescafe (200g)", 10, 15, 5, 20),
        ("Maggi Cubes", 80, 100, 60, 120),
    ]
    # November carries no opening column: supplied, sold and the book's closing.
    november = [
        ("Indomie (Chicken)", 30, 10, 80),
        ("Peak Milk (500g)", 40, 20, 80),
        ("Golden Morn", 20, 10, 65),
        ("Eva Water (75cl)", 80, 30, 190),
        ("Milo (400g)", 25, 10, 50),
        ("Soya Beans", 150, 40, 330),
        ("Tomato Paste", 50, 20, 105),
        ("Dan-Q Noodles", 40, 20, 65),
        ("Peak Milk (1L)", 30, 15, 60),
        ("Dettol (750ml)", 20, 10, 37),
    ]
    rows = [
        StockRow(
            item=item,
            block="October 2025",
            opening=opening,
            supplied=supplied,
            sold=sold,
            left=left,
            confidence=0.95,
        )
        for item, opening, supplied, sold, left in october
    ]
    rows += [
        StockRow(
            item=item,
            block="November 2025",
            opening=None,
            supplied=supplied,
            sold=sold,
            left=left,
            confidence=0.95,
        )
        for item, supplied, sold, left in november
    ]
    data: dict[str, Any] = {
        "shop": "Ada's General Trading",
        "date": "October and November 2025",
        "rows": rows,
        "notes": ["Restock Milo and Dettol next week."],
    }
    data.update(overrides)
    return StockBook.model_validate(data)
