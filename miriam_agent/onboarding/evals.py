"""Behavioral evaluation for the onboarding conductor.

Runs recorded onboarding turns through the deterministic spec linter
(``quality.evaluate_reply``) so a drift in Miriam's voice and guardrails fails
loudly, the way a behavioral eval should. Two runners:

  - scenario replay: the curated conversations in :data:`SCENARIOS` are each
    linted in full (every turn must pass).
  - trace replay: a JSONL file of recorded real turns is replayed and scored
    against a pass threshold (default 1.0 = no violations allowed).

Manual run:

    python -m miriam_agent.onboarding.evals
"""

from __future__ import annotations

import json
import os
from typing import Any

from miriam_agent.onboarding.quality import EvalMeta, evaluate_reply

# Curated conversations that must lint clean: equal parts the conversational
# arc and the guardrails; a turn may carry a ``grounded`` text that every
# number in its reply must appear in (spec §30). If one of these replies
# drifts, the eval points at the section it violates.
SCENARIOS: list[tuple[str, list[dict[str, Any]]]] = [
    (
        "money moment opener",
        [
            {
                "reply": (
                    "What's been on your mind about money lately - I want "
                    "the real stuff, not the polite version."
                ),
                "intent": "interview",
            },
        ],
    ),
    (
        "reflection then probe",
        [
            {
                "reply": (
                    "So the month runs out before the money does, and that's "
                    "been going on a while. When income lands, does it come "
                    "steady or in lumps?"
                ),
                "intent": "interview",
            },
        ],
    ),
    (
        "statement request",
        [
            {
                "reply": (
                    "A real statement would make this sharper. Send one over "
                    "- a PDF works - or just say skip, and we go with what "
                    "you've told me."
                ),
                "intent": "request_statement",
            },
        ],
    ),
    (
        "plan presentation",
        [
            {
                "reply": (
                    "Here's your picture: the buffer runs out before the "
                    "month does. Two moves first - lock the next month away "
                    "the moment income lands, then split the lumpy income "
                    "into smaller pieces. Shall I set this up so it runs "
                    "quietly in the background for you?"
                ),
                "intent": "present_plan",
                "present": True,
            },
        ],
    ),
    (
        "consent to automation",
        [
            {
                "reply": (
                    "Done - this is now how I work for you. One slice of "
                    "every income goes to the buffer before anything else "
                    "gets its turn. You stay the one who decides."
                ),
                "intent": "consent_yes",
            },
        ],
    ),
    (
        "tap reply",
        [
            {
                "reply": "Nervous or excited?",
                "intent": "interview",
                "has_taps": True,
            },
        ],
    ),
    (
        "directness escalation",
        [
            {
                "reply": (
                    "I'm going to be blunt - the bills going out on time "
                    "while the money still disappears is the whole story. "
                    "We've seen this happen more than once now."
                ),
                "intent": "interview",
            },
        ],
    ),
    (
        "restates grounded numbers",
        [
            {
                "reply": (
                    "So roughly 4,000 comes in, 800 goes to rent - and the "
                    "month still runs out. When the lump lands, what happens "
                    "first?"
                ),
                "intent": "interview",
                "grounded": (
                    "cashflow: roughly 4000/month rent: 800/month "
                    "the month runs out before the money does"
                ),
            },
        ],
    ),
]


def run_replay(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lint a list of recorded turns; one result dict per turn."""
    results: list[dict[str, Any]] = []
    for index, turn in enumerate(turns):
        reply = str(turn.get("reply") or "")
        meta = EvalMeta(
            intent=str(turn.get("intent") or "interview"),
            has_taps=bool(turn.get("has_taps")),
            present=bool(turn.get("present")),
            grounded=str(turn.get("grounded") or ""),
        )
        violations = evaluate_reply(reply, meta)
        results.append(
            {
                "index": index,
                "reply": reply,
                "violations": violations,
                "ok": not violations,
            }
        )
    return results


def _load_trace(path: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            turns.append(json.loads(line))
    return turns


def evaluate_trace_file(path: str, threshold: float = 1.0) -> dict[str, Any]:
    """Replay a trace fixture and score it against a pass ratio.

    Returns a summary with per-turn ``results``; ``ok`` is True when the
    passed ratio clears ``threshold``.
    """
    turns = _load_trace(path)
    results = run_replay(turns)
    total = len(results)
    passed = sum(1 for result in results if result["ok"])
    ratio = (passed / total) if total else 1.0
    return {
        "total": total,
        "passed": passed,
        "ratio": ratio,
        "threshold": threshold,
        "ok": ratio >= threshold,
        "results": results,
    }


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", ".."))
    fixture = os.path.join(root, "tests", "evals", "fixtures", "traces.jsonl")

    print("trace fixture:", fixture)
    trace = evaluate_trace_file(fixture, threshold=1.0)
    for result in trace["results"]:
        status = "ok" if result["ok"] else "FAIL " + ",".join(result["violations"])
        print(f"  [{status}] {result['reply'][:80]}")
    print(
        f"trace: passed {trace['passed']}/{trace['total']} "
        f"(threshold {trace['threshold']})"
    )

    all_ok = trace["ok"]
    for name, turns in SCENARIOS:
        results = run_replay(turns)
        fails = [result for result in results if not result["ok"]]
        if fails:
            all_ok = False
            for result in fails:
                print(
                    f"  scenario '{name}' FAIL "
                    f"{','.join(result['violations'])}: {result['reply'][:80]}"
                )
        else:
            print(f"  scenario '{name}': pass")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
