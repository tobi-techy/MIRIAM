"""Two-pass vision reading over llmapi.ai.

Pass (a) copies exactly what is written and calculates nothing. Pass (b) sends
the same image plus pass (a)'s JSON and asks only which values differ, pointing
at a specific cell. A correction is accepted only when it names a cell; every
disagreement becomes a low-confidence flag, and numbers are never averaged.

The provider is injected so tests read a fixed answer. Model ids, reasoning
effort and the price table live in settings and ``agents.llm``; nothing about a
model id is hardcoded here.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from miriam_agent.agents.llm import (
    ChatMessage,
    LLMAPIProvider,
    get_llmapi_provider,
)
from miriam_agent.config.settings import get_settings
from miriam_agent.core.exceptions import AgentError
from miriam_agent.shop.preprocess import PreparedDocument
from miriam_agent.shop.schemas import SCHEMA_BY_KIND, OtherDocument

logger = logging.getLogger(__name__)


class ReadCapExceeded(Exception):
    """The per-document extraction cap was hit. Fail closed, never partial."""


class ReadFailed(Exception):
    """Every tier failed. Fail closed with the retake message."""


@dataclass
class ReadBudget:
    """Shared cap on extraction reads for one document."""

    max_extractions: int = 2
    used: int = 0

    def spend(self) -> None:
        if self.used >= self.max_extractions:
            raise ReadCapExceeded(f"extraction cap {self.max_extractions} reached")
        self.used += 1


@dataclass(frozen=True)
class ReadResult:
    schema_kind: str
    extraction: BaseModel
    model_path: list[str] = field(default_factory=list)
    reads_used: int = 0
    flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    degraded: bool = False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CellCorrection(_Strict):
    path: str = ""
    value: str = ""


class Verification(_Strict):
    corrections: list[CellCorrection] = Field(default_factory=list)


_EXTRACT_SYSTEM = (
    "You read a Nigerian shop document from an image. Copy exactly what is "
    "written. Do not calculate, correct or tidy any number: if the book says "
    "N24,800, you write N24,800 even when the multiplication looks wrong. "
    "Money is a decimal string in naira with no commas and no currency symbol "
    "(write 9800, not N9,800). Keep any crossed-out value as both the old and "
    "the new number rather than choosing one. Mark a cell unclear by naming it "
    "in that row's `unclear` list and lower the row `confidence` (0 to 1). "
    "Capture margin notes and any free-text comment as raw text in `notes`. "
    "Return only JSON that matches the schema."
)

_VERIFY_SYSTEM = (
    "You compare a JSON extraction against the same image. Return only the "
    "cells that differ from the image, each as a `path` and a corrected "
    "`value`. A path names one cell, for example rows[2].left or "
    "lines[0].line_total_naira. Do not restate matching values, do not explain "
    "and do not calculate. If every value matches, return an empty list."
)


def _data_urls(prepared: PreparedDocument) -> list[str]:
    return [
        "data:image/png;base64," + base64.b64encode(page.png).decode("ascii")
        for page in prepared.pages
    ]


def _context_block(prepared: PreparedDocument, caption: str) -> str:
    parts: list[str] = []
    if caption.strip():
        parts.append(f"Caption the owner sent with it: {caption.strip()}")
    if prepared.native_text_usable and prepared.native_text.strip():
        parts.append(
            "Text already extracted from this PDF (use it, but still check the "
            "image):\n" + prepared.native_text.strip()[:6000]
        )
    return "\n\n".join(parts)


def schema_for_kind(kind: str) -> type[BaseModel]:
    """The pydantic model a document kind is read with."""
    return SCHEMA_BY_KIND.get(kind, OtherDocument)


def _extract_messages(
    prepared: PreparedDocument, schema: type[BaseModel], caption: str
) -> list[ChatMessage]:
    context = _context_block(prepared, caption)
    user = (
        "Read this document and fill the schema. " f"Schema name: {schema.__name__}.\n"
    )
    if context:
        user = f"{user}\n{context}"
    return [
        ChatMessage(role="system", content=_EXTRACT_SYSTEM),
        ChatMessage(role="user", content=user, images=_data_urls(prepared)),
    ]


def _verify_messages(
    prepared: PreparedDocument, extraction: BaseModel, caption: str
) -> list[ChatMessage]:
    payload = extraction.model_dump_json()
    user = (
        "Here is the extraction as JSON. List only the cells that differ from "
        f"the image.\n\n{payload}"
    )
    return [
        ChatMessage(role="system", content=_VERIFY_SYSTEM),
        ChatMessage(role="user", content=user, images=_data_urls(prepared)),
    ]


_PATH_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)((?:\[\d+\]|[.][A-Za-z_][A-Za-z0-9_]*)*)$"
)


def _split_path(path: str) -> list[str | int] | None:
    match = _PATH_RE.match(path.strip())
    if not match:
        return None
    keys: list[str | int] = [match.group(1)]
    rest = match.group(2)
    for token in re.findall(r"\[(\d+)\]|\.([A-Za-z_][A-Za-z0-9_]*)", rest):
        idx, name = token
        keys.append(int(idx) if idx else name)
    return keys


def _apply_correction(data: Any, keys: list[str | int], value: str) -> bool:
    """Set one cell. Returns False when the path does not exist."""
    node = data
    for key in keys[:-1]:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return False
    last = keys[-1]
    try:
        node[last] = value
    except (KeyError, IndexError, TypeError):
        return False
    return True


def _coerce_value_for_field(
    model: type[BaseModel], keys: list[str | int], value: str
) -> Any:
    """Keep the correction the same JSON type the field expects."""
    field_name = keys[-1]
    if isinstance(field_name, str) and field_name in getattr(model, "model_fields", {}):
        annotation = str(model.model_fields[field_name].annotation)
        if "int" in annotation and "None" not in annotation:
            try:
                return int(value)
            except ValueError:
                return value
    # Numeric-looking strings for optional int cells become ints where the
    # field allows it; the pydantic re-validation below is the final gate.
    if re.fullmatch(r"-?\d+", value or ""):
        return int(value)
    return value


@dataclass
class _TierAttempt:
    extraction: BaseModel
    flags: list[str]
    confidence: float
    corrections: int


async def _one_read(
    provider: LLMAPIProvider,
    prepared: PreparedDocument,
    schema: type[BaseModel],
    *,
    caption: str,
    reasoning_effort: str | None,
    budget: ReadBudget,
    tier_label: str,
) -> tuple[BaseModel, list[str], float, list[str]]:
    """Pass (a) extract, then pass (b) verify. One schema retry, else fail."""
    flags: list[str] = []
    model_path: list[str] = [tier_label]

    budget.spend()
    extraction = await _extract_once(
        provider, prepared, schema, caption, reasoning_effort, flags, budget
    )
    model_path.append(f"{tier_label}:verify")

    # Pass (b): corrections only where a specific cell is named.
    corrections_applied = 0
    try:
        verify_messages = _verify_messages(prepared, extraction, caption)
        response = await provider.complete_structured(
            verify_messages,
            Verification,
            reasoning_effort=reasoning_effort,
            max_tokens=2000,
        )
        verification = Verification.model_validate_json(response.content)
    except (AgentError, ValidationError) as e:
        logger.warning("verify pass failed, keeping extraction: %s", e)
        flags.append("verify pass unavailable")
        verification = Verification()

    data = extraction.model_dump()
    for correction in verification.corrections:
        keys = _split_path(correction.path)
        if not keys or not _apply_correction(
            data, keys, _coerce_value_for_field(schema, keys, correction.value)
        ):
            flags.append(f"verify pointed at an unknown cell: {correction.path}")
            continue
        corrections_applied += 1
    if corrections_applied:
        flags.append(f"verify corrected {corrections_applied} cell(s)")
        try:
            extraction = schema.model_validate(data)
        except ValidationError as e:
            logger.warning("corrected extraction invalid, keeping original: %s", e)
            flags.append("verify correction rejected by schema")

    confidence = _mean_confidence(extraction)
    return extraction, flags, confidence, model_path


async def _extract_once(
    provider: LLMAPIProvider,
    prepared: PreparedDocument,
    schema: type[BaseModel],
    caption: str,
    reasoning_effort: str | None,
    flags: list[str],
    budget: ReadBudget,
) -> BaseModel:
    messages = _extract_messages(prepared, schema, caption)
    response = await provider.complete_structured(
        messages,
        schema,
        reasoning_effort=reasoning_effort,
        max_tokens=4000,
    )
    try:
        return schema.model_validate_json(response.content)
    except ValidationError as e:
        logger.warning("extraction failed validation, retrying once: %s", e)
        flags.append("schema retry")
        budget.spend()
        retry_response = await provider.complete_structured(
            messages, schema, reasoning_effort=reasoning_effort, max_tokens=4000
        )
        try:
            return schema.model_validate_json(retry_response.content)
        except ValidationError as retry_error:
            raise ReadFailed(
                "I couldn't read that page. Try a clearer photo, top down, "
                "with the whole table in view."
            ) from retry_error


def _mean_confidence(extraction: BaseModel) -> float:
    rows = getattr(extraction, "rows", None) or getattr(extraction, "lines", None)
    if not rows:
        return 1.0
    values = [float(getattr(r, "confidence", 1.0) or 0.0) for r in rows]
    if not values:
        return 1.0
    return sum(values) / len(values)


def _effort_for(tier: str) -> str:
    settings = get_settings()
    return (
        settings.READ_SMALL_REASONING_EFFORT
        if tier == "small"
        else settings.READ_BIG_REASONING_EFFORT
    )


def _model_for(tier: str) -> str:
    settings = get_settings()
    return settings.READ_SMALL_MODEL if tier == "small" else settings.READ_BIG_MODEL


async def read_on_tier(
    prepared: PreparedDocument,
    schema: type[BaseModel],
    tier: str,
    *,
    caption: str = "",
    budget: ReadBudget | None = None,
    provider: LLMAPIProvider | None = None,
) -> tuple[BaseModel, list[str], float, list[str]]:
    """One extraction read (both passes) on one tier."""
    budget = budget if budget is not None else ReadBudget()
    provider = provider or get_llmapi_provider(_model_for(tier))
    return await _one_read(
        provider,
        prepared,
        schema,
        caption=caption,
        reasoning_effort=_effort_for(tier),
        budget=budget,
        tier_label=tier,
    )


async def read_document(
    prepared: PreparedDocument,
    schema_kind: str,
    starting_tier: str,
    *,
    caption: str = "",
    budget: ReadBudget | None = None,
    provider_factory: Any = None,
) -> ReadResult:
    """Read one document, trying the other tier once on a provider failure."""
    budget = (
        budget
        if budget is not None
        else ReadBudget(max_extractions=get_settings().SHOP_READ_MAX_EXTRACTIONS)
    )
    schema = schema_for_kind(schema_kind)
    provider_factory = provider_factory or (
        lambda tier: get_llmapi_provider(_model_for(tier))  # type: ignore[arg-type]
    )

    tiers = [starting_tier] + [t for t in ("small", "big") if t != starting_tier]
    flags: list[str] = []
    last_error: Exception | None = None
    attempted: list[str] = []

    for tier in tiers[:2]:
        try:
            extraction, read_flags, confidence, model_path = await read_on_tier(
                prepared,
                schema,
                tier,
                caption=caption,
                budget=budget,
                provider=provider_factory(tier),
            )
            flags.extend(read_flags)
            return ReadResult(
                schema_kind=schema_kind,
                extraction=extraction,
                model_path=attempted + model_path,
                reads_used=budget.used,
                flags=flags,
                confidence=confidence,
                degraded=budget.used > 1,
            )
        except ReadCapExceeded:
            raise
        except (AgentError, ReadFailed) as e:
            last_error = e
            attempted.append(f"{tier}:failed")
            logger.warning("read on tier %s failed: %s", tier, e)
            continue

    if isinstance(last_error, ReadFailed):
        raise last_error
    raise ReadFailed(
        "I couldn't read that page. Try a clearer photo, top down, "
        "with the whole table in view."
    )


__all__ = [
    "CellCorrection",
    "ReadBudget",
    "ReadCapExceeded",
    "ReadFailed",
    "ReadResult",
    "Verification",
    "read_document",
    "read_on_tier",
    "schema_for_kind",
]
