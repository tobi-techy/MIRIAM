"""Per-claim egress verification.

The holistic ``invents_facts`` head remains a backstop. Once that head says a
draft is plausibly sendable, this module decomposes the draft into a small set
of material claims and asks one narrow Noul per claim. The Noul is framed so
``true`` means the claim is unsupported. A single confident unsupported claim
regenerates the draft; the checks are not averaged into silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from pydantic import create_model
from typesafe_sdk import Noul, NoulAnswer, SystemOneResponse

from miriam_agent.judgment.questions import Catalog

MAX_CLAIMS = 6
MAX_CLAIM_CHARS = 400
_FACT_NUMBER_RE = re.compile(r"(?:[$₦€£]?\s?\d[\d,]*(?:\.\d+)?%?|\b\d+%)")
_FACT_WORDS_RE = re.compile(
    r"\b(balance|spent|spend|paid|payment|rate|transaction|receipt|"
    r"position|holding|return|income|salary|bill|transfer|fee|charge|"
    r"deposit|withdraw|saved|savings|earned|lost|owe|debt)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


@dataclass(frozen=True)
class ClaimCheck:
    """One material claim and the question id that verifies it."""

    question_id: str
    claim: str


def extract_claims(draft: str) -> list[str]:
    """Return the material factual sentences in a draft.

    The extraction is deliberately conservative: one sentence becomes a claim
    only when it contains a number or a financial-fact keyword. This keeps the
    claim battery cheap enough to run on every sendable egress draft.
    """
    claims: list[str] = []
    seen: set[str] = set()
    for raw in _SENTENCE_SPLIT_RE.split(draft or ""):
        sentence = _BULLET_RE.sub("", " ".join(raw.split())).strip()
        if not sentence:
            continue
        if not (_FACT_NUMBER_RE.search(sentence) or _FACT_WORDS_RE.search(sentence)):
            continue
        sentence = sentence[:MAX_CLAIM_CHARS]
        if sentence in seen:
            continue
        seen.add(sentence)
        claims.append(sentence)
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def build_claim_catalog(draft: str) -> tuple[Catalog, list[ClaimCheck]]:
    """Build one Noul per material claim against the supplied draft."""
    claims = extract_claims(draft)
    checks = [
        ClaimCheck(question_id=f"claim_{index}_unsupported", claim=claim)
        for index, claim in enumerate(claims)
    ]
    questions = {
        check.question_id: Noul(
            instructions=(
                "Is this claim unsupported by `supporting_context`, `history`, "
                f"or `turn.user_text`? Claim: {check.claim}"
            ),
            criteria={
                "true": (
                    "The claim states a number, balance, transaction, rate, "
                    "date, holding, or event that cannot be found in the "
                    "supplied evidence, or was only asserted by the assistant "
                    "earlier."
                ),
                "false": (
                    "The claim is supported by a named source in "
                    "`supporting_context`, the user's current message, or a user "
                    "statement in `history`."
                ),
            },
        )
        for check in checks
    }
    fields: dict[str, Any] = {check.question_id: (NoulAnswer, ...) for check in checks}
    response_model = cast(
        type[SystemOneResponse],
        create_model(
            "ClaimEgressJudgment",
            __base__=SystemOneResponse,
            **fields,
        ),
    )
    catalog = Catalog(
        name="egress_claims",
        version="1",
        questions=questions,
        response_model=response_model,
    )
    return catalog, checks


def unsupported_claims(
    judgment: SystemOneResponse,
    checks: list[ClaimCheck],
    *,
    threshold: float,
) -> list[str]:
    """Return claims whose unsupported probability clears the action bar."""
    unsupported: list[str] = []
    for check in checks:
        answer = getattr(judgment, check.question_id, None)
        if answer is not None and answer.noul >= threshold:
            unsupported.append(check.claim)
    return unsupported


__all__ = [
    "ClaimCheck",
    "build_claim_catalog",
    "extract_claims",
    "unsupported_claims",
]
