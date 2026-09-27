"""Small read helpers for TypeSafe typed answers."""

from __future__ import annotations

from typing import Any


def choice_margin(answer: Any) -> float:
    """Return top-probability minus runner-up for a Choice answer.

    Older recorded fixtures and test doubles may not carry a full distribution;
    an absent distribution is treated as a clean margin rather than as evidence
    against the answer.
    """
    probabilities = getattr(answer, "probabilities", None) or {}
    try:
        values = sorted(
            (float(value) for value in probabilities.values()), reverse=True
        )
    except (TypeError, ValueError):
        return 1.0
    if len(values) < 2:
        return 1.0
    return max(0.0, values[0] - values[1])


__all__ = ["choice_margin"]
