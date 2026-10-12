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
import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

    @field_validator("value", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> Any:
        """A correction that arrives as a JSON number is still a value.

        Strict ``str`` would reject ``27`` and take the whole verify pass down
        with it, losing every other correction in the same answer.
        """
        if isinstance(v, bool):
            return str(v).lower()
        if isinstance(v, (int, float)):
            return str(v)
        return v


class Verification(_Strict):
    corrections: list[CellCorrection] = Field(default_factory=list)


_EXTRACT_SYSTEM = (
    "You read a Nigerian shop document from an image. Copy exactly what is "
    "written. Do not calculate, correct or tidy any number: if the book says "
    "N24,800, you write N24,800 even when the multiplication looks wrong, and "
    "if a line reads 6 x N3,800 with an amount of N24,800 you write 24800 as "
    "the amount even though you know 6 x 3,800 is 22,800. Never reconcile "
    "printed arithmetic, and never substitute your own product. "
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


def _path_exists(data: Any, keys: list[str | int]) -> bool:
    """True when the path names a cell that is actually there."""
    node = data
    for key in keys:
        if isinstance(node, dict) and isinstance(key, str):
            if key not in node:
                return False
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int):
            if not 0 <= key < len(node):
                return False
            node = node[key]
        else:
            return False
    return True


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


def _unwrap_annotation(annotation: Any) -> Any:
    """``list[X]`` / ``dict[str, X]`` / ``X | None`` -> ``X``, recursively."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if not args:
        return annotation
    if origin in (list, set, frozenset, tuple):
        return _unwrap_annotation(args[0])
    if origin is dict:
        return _unwrap_annotation(args[-1])
    return _unwrap_annotation(args[0]) if len(args) == 1 else args[0]


def _leaf_annotation(model: type[BaseModel], keys: list[str | int]) -> Any:
    """The declared type of the last cell in a path like ``rows[3].crossed_out.new``.

    Walking the path matters: a correction aimed at a nested cell must be
    coerced by that cell's own type, not by the root model's. Looking ``new`` up
    on ``StockBook`` finds nothing, which used to fall through to "the value
    looks numeric, make it an int" and handed ``crossed_out.new`` an int, so
    pydantic rejected the whole corrected extraction and a real correction was
    silently dropped.
    """
    annotation: Any = model
    for key in keys:
        current = _unwrap_annotation(annotation)
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return current
        if isinstance(key, int):
            # An index does not change the item type: list[X] is unwrapped.
            continue
        field = current.model_fields.get(str(key))
        if field is None:
            return Any
        annotation = field.annotation
    return _unwrap_annotation(annotation)


def _coerce_value_for_field(
    model: type[BaseModel], keys: list[str | int], value: Any
) -> Any:
    """Keep the correction the same JSON type the field it names expects."""
    leaf = _leaf_annotation(model, keys)
    text = value if isinstance(value, str) else str(value)
    if leaf is str:
        # A crossed-out "27" is text, not a count. Coercing it to int is what
        # made the corrected extraction fail validation.
        return text
    if leaf is int:
        try:
            return int(text.strip())
        except ValueError:
            return text
    if leaf is float:
        try:
            return float(text.strip())
        except ValueError:
            return text
    if leaf is bool:
        return text.strip().lower() in ("true", "yes", "1")
    return text


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

    original = extraction.model_dump()
    candidates: list[tuple[list[str | int], Any, str]] = []
    for correction in verification.corrections:
        keys = _split_path(correction.path)
        if not keys or not _path_exists(original, keys):
            flags.append(f"verify pointed at an unknown cell: {correction.path}")
            continue
        candidates.append(
            (
                keys,
                _coerce_value_for_field(schema, keys, correction.value),
                correction.path,
            )
        )

    if candidates:
        # The common case: the whole batch is valid, so apply it in one go.
        batch = copy.deepcopy(original)
        for keys, value, _ in candidates:
            _apply_correction(batch, keys, value)
        try:
            extraction = schema.model_validate(batch)
            flags.append(f"verify corrected {len(candidates)} cell(s)")
        except ValidationError as e:
            # One correction that cannot be valid must not roll back the rest.
            # A live read hit exactly this: the verify pass offered "10 ctn" for
            # an int qty, and every other correction in the same answer was
            # thrown away with it.
            logger.warning("verify batch rejected, applying one at a time: %s", e)
            kept = 0
            current = copy.deepcopy(original)
            for keys, value, path in candidates:
                trial = copy.deepcopy(current)
                _apply_correction(trial, keys, value)
                try:
                    schema.model_validate(trial)
                except ValidationError:
                    flags.append(f"verify correction dropped: {path}")
                    continue
                current = trial
                kept += 1
            extraction = schema.model_validate(current)
            if kept:
                flags.append(f"verify corrected {kept} cell(s)")

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
