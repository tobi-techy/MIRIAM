"""Layer 3 - VOICE. Read-only narration, with the clamp.

Voice is called only after Hands has built STATE and Judgment has filled
``STATE.decision``. It is never called to decide anything, and it has nothing to
call: the provider is invoked with the user's messages and ``tools=None``, so
there is no tool surface through which a sentence could become a movement.

Three checks stand between the model's words and the user:

* **the number clamp.** Every material figure in the reply must already appear
  in STATE. A figure the pipeline never produced means the reply is discarded
  and the deterministic line is sent instead. This is what makes "Voice never
  does arithmetic" a fact rather than an instruction.
* **the word ceiling.** A wall of text is not narration, so an over-long reply
  falls back to the deterministic line the same way.
* **the confirm parse.** ``CONFIRM:`` is only honoured when it carries
  ``WAIT``, ``NO``, or the confirm id that is actually in STATE. A model that
  writes ``CONFIRM: yes`` produces no confirmation, because "yes" is not an id.

Everything fails open to the deterministic line: a dead model costs wording,
never correctness. That is also why killing the model cannot stop a split, which
Hands does without Voice at all.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from miriam_agent.agents.llm import LLMProvider, get_llm_provider
from miriam_agent.hands.state import HandlerState
from miriam_agent.voice.prompt import build_voice_messages

logger = logging.getLogger(__name__)

# Any figure at or above this is a claim about money and must be traceable to
# STATE. Below it, numbers are structural ("9 days", "one question") and
# blocking them would make the copy unusable for no safety gain.
MATERIAL_NUMBER = Decimal("100")

# The hard ceiling on a narration that is not a requested breakdown.
WORD_CEILING = 80

# What the user is told when the ledger could not be read, so nothing was
# decided and nothing moved. It is a fixed line rather than a narration because
# there is no STATE to narrate from: this is the one case where Voice says
# something without a decision behind it, and it says only that it could not.
UNAVAILABLE_LINE = "I could not complete that. Try again."

_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)(\s*%)?")
_CONFIRM_RE = re.compile(r"^\s*CONFIRM:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)

VoiceStatus = Literal[
    "generated",  # the model's words passed every check
    "clamped",  # the model's words were replaced by the deterministic line
    "unavailable",  # the provider failed; the deterministic line was sent
]


class VoiceMessage(BaseModel):
    """One narrated message, and what had to be done to make it safe."""

    model_config = ConfigDict(extra="forbid")

    text: str
    status: VoiceStatus = "generated"
    confirm: str = ""
    model: str = ""
    violations: list[str] = Field(default_factory=list)


def _normalize_number(raw: str) -> str:
    text = raw.replace(",", "").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _numbers_in(text: str) -> set[str]:
    return {_normalize_number(m.group(1)) for m in _NUMBER_RE.finditer(text or "")}


def _material_numbers(text: str) -> set[str]:
    """Figures in ``text`` that constitute a claim about money or a rate."""
    found: set[str] = set()
    for match in _NUMBER_RE.finditer(text or ""):
        raw = _normalize_number(match.group(1))
        if match.group(2) is not None:
            found.add(raw)
            continue
        try:
            if Decimal(raw) >= MATERIAL_NUMBER:
                found.add(raw)
        except InvalidOperation:
            continue
    return found


def _decimal_forms(value: Decimal) -> set[str]:
    """Every reasonable written form of a figure STATE holds."""
    forms = {_normalize_number(str(value))}
    for places in ("1", "0.1", "0.01"):
        try:
            forms.add(_normalize_number(str(value.quantize(Decimal(places)))))
        except (ArithmeticError, InvalidOperation):
            continue
    try:
        forms.add(_normalize_number(str(value.to_integral_value())))
    except (ArithmeticError, InvalidOperation):
        pass
    return forms


def _walk_numbers(node: Any, found: set[Decimal]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk_numbers(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_numbers(value, found)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, (int, float, Decimal)):
        found.add(Decimal(str(node)))
    elif isinstance(node, str):
        stripped = node.strip()
        if not stripped:
            return
        try:
            found.add(Decimal(stripped.replace(",", "")))
        except InvalidOperation:
            return


def state_vocabulary(state: HandlerState) -> set[str]:
    """Every number STATE already contains, in every form it might be written.

    STATE is the vocabulary. If a figure is not in here, Voice does not know it,
    and the clamp rejects it rather than letting a plausible number through.
    """
    found: set[Decimal] = set()
    _walk_numbers(json.loads(json.dumps(state.to_dict(), default=str)), found)
    allowed: set[str] = set()
    for value in found:
        allowed |= _decimal_forms(value)
    return allowed


def clamp_violations(state: HandlerState, text: str) -> list[str]:
    """Figures in a draft that STATE never contained."""
    allowed = state_vocabulary(state)
    return sorted(n for n in _material_numbers(text) if n not in allowed)


def extract_confirm(text: str, state: HandlerState) -> tuple[str, str | None]:
    """Split an optional ``CONFIRM:`` line off a draft.

    Returns ``(text_without_the_line, confirm_or_None)``. A value that is not
    ``WAIT``, ``NO``, or the id actually in STATE is refused: an unstructured
    "yes" must never be read as a confirmation.
    """
    match = _CONFIRM_RE.search(text or "")
    if match is None:
        return text, None
    candidate = match.group(1).strip().strip(".,;")
    cleaned = _CONFIRM_RE.sub("", text).strip()
    decision = state.decision or {}
    valid = {"WAIT", "NO", "wait", "no"}
    if candidate in valid:
        return cleaned, candidate.upper()
    confirm_id = str(decision.get("confirm_id") or "")
    if confirm_id and candidate == confirm_id:
        return cleaned, confirm_id
    logger.warning("refusing an unstructured confirmation %r", candidate)
    return cleaned, None


# ---------------------------------------------------------------------------
# The deterministic line
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    """A figure read out of STATE, never computed."""
    if value is None:
        return ""
    try:
        return f"{Decimal(str(value)):,f}".rstrip("0").rstrip(".")
    except InvalidOperation:
        return str(value)


def deterministic_message(state: HandlerState) -> str:
    """The line Voice would have written, from STATE alone.

    This is the fallback for every failure, and it is deliberately good enough
    to ship: it says the number, the verdict, and the reason, in that order.
    """
    decision = state.decision or {}
    reasons = [str(r) for r in (decision.get("reasons") or [])]
    parts: list[str] = []

    if state.execution is not None:
        execution = state.execution
        amount = _fmt(execution.amount)
        to = f" to {execution.counterparty}" if execution.counterparty else ""
        parts.append(
            f"{execution.action} {amount} {state.currency}{to} ({execution.status})."
        )
        parts.append(f"Spendable is now {_fmt(state.spendable)} {state.currency}.")
    elif state.pending_inflow is not None:
        amount = _fmt(state.pending_inflow.amount)
        classified = state.pending_inflow.classified_as or "unknown"
        parts.append(
            f"{amount} {state.currency} came in and was split on the "
            f"{state.track.name} track, classified {classified}."
        )
        parts.append(f"Spendable is {_fmt(state.spendable)} {state.currency}.")
    else:
        parts.append(f"Spendable is {_fmt(state.spendable)} {state.currency}.")
        rent = state.rent_first
        if rent.required > 0 and rent.due_in_days is not None:
            parts.append(
                f"Rent is {_fmt(rent.required)} {state.currency} in "
                f"{rent.due_in_days} days."
            )
        action = state.proposed_action
        if action is not None and action.amount is not None:
            parts.append(f"You asked for {_fmt(action.amount)} {state.currency}.")

    if reasons:
        parts.append("Why: " + ", ".join(reasons) + ".")

    confirm = str(decision.get("confirm_id") or "")
    if confirm:
        parts.append(f"CONFIRM: {confirm}")
    elif decision.get("next_mode") == "ask":
        parts.append("CONFIRM: WAIT")
    elif decision.get("action_choice") in ("deny", "defer"):
        parts.append("CONFIRM: NO")

    return " ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# speak
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    return len(text.split())


async def speak(
    *,
    state: HandlerState,
    utterance: str = "",
    provider: LLMProvider | None = None,
    facts: list[dict[str, Any]] | None = None,
    breakdown: bool = False,
) -> VoiceMessage | None:
    """Narrate STATE. Returns ``None`` when Voice must not be called at all.

    ``None`` means the turn is silent by design (``stay_quiet`` with nothing
    executed), not that narration failed. A failure returns the deterministic
    line with ``status="unavailable"``.
    """
    decision = state.decision
    if decision is None:
        # Judgment has not run, so there is no verdict to lead with.
        return None
    if decision.get("next_mode") == "stay_quiet" and state.execution is None:
        return None

    fallback = deterministic_message(state)

    if provider is None:
        try:
            provider = get_llm_provider()
        except Exception as exc:  # noqa: BLE001 - a dead model costs wording only
            logger.info("voice unavailable: %s", exc)
            return VoiceMessage(
                text=fallback, status="unavailable", violations=[str(exc)]
            )

    messages = build_voice_messages(state, utterance, facts, breakdown=breakdown)
    try:
        # tools is deliberately omitted: Voice has no tool surface at all.
        response = await provider.complete(messages, temperature=0.4, max_tokens=400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice call failed: %s", exc)
        return VoiceMessage(text=fallback, status="unavailable", violations=[str(exc)])

    model = str(getattr(response, "model", "") or "")
    draft = str(getattr(response, "content", "") or "").strip()
    if not draft:
        return VoiceMessage(
            text=fallback,
            status="unavailable",
            model=model,
            violations=["empty_reply"],
        )

    cleaned, confirm = extract_confirm(draft, state)
    violations = clamp_violations(state, cleaned)
    if not breakdown and _word_count(cleaned) > WORD_CEILING:
        violations.append(f"over_{WORD_CEILING}_words")
    if not cleaned:
        violations.append("nothing_left_after_confirm_parse")

    if violations:
        logger.warning("voice reply clamped: %s", violations)
        return VoiceMessage(
            text=fallback,
            status="clamped",
            confirm=confirm or "",
            model=model,
            violations=violations,
        )

    return VoiceMessage(
        text=cleaned, status="generated", confirm=confirm or "", model=model
    )


__all__ = [
    "MATERIAL_NUMBER",
    "WORD_CEILING",
    "VoiceMessage",
    "clamp_violations",
    "deterministic_message",
    "extract_confirm",
    "speak",
    "state_vocabulary",
]
