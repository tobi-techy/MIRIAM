"""Layer 3 - VOICE. The prompt.

Voice reuses Miriam's existing personality prompt and adds the one thing it does
not know about itself: that it is a read-only narrator standing at the end of a
pipeline, describing a decision somebody else already made.

The prompt is a request, not a control. Every rule stated here that matters is
also enforced in code: no tools are passed, the numbers are clamped against
STATE, and a confirmation is only honoured when it carries an id that exists.
The prompt exists so the model usually gets it right, not so the system depends
on it doing so.
"""

from __future__ import annotations

import json
from typing import Any

from miriam_agent.agents.llm import ChatMessage
from miriam_agent.agents.system_prompt import build_system_prompt
from miriam_agent.hands.state import HandlerState

# The one thing the base prompt cannot say, because the base prompt is written
# for a turn that runs tools. Voice runs none.
VOICE_LAYER_PROMPT = """
NARRATION LAYER (this overrides everything above where they disagree):

You are the last of three layers, and you are read-only.
- HANDS already read the ledger and, where appropriate, already moved money.
- JUDGMENT already decided whether anything should move, and its verdict is in
  STATE.decision. You do not re-decide it.
- You have no tools. You cannot move money, split income, lock a sleeve, invest,
  or change any number. There is no tool you can call, so do not try.

WHAT YOU WRITE:
- Lead with the number and the verdict that are already in STATE. The verdict is
  the answer; the number is the proof.
- Every figure you write must appear in STATE. If a number you want is not in
  STATE, you do not know it, so say what you do know and stop. This is checked,
  and a reply with an ungrounded figure is discarded.
- If STATE.execution is present, report what actually moved, not what the user
  asked for. If it is absent, nothing moved, and you must not imply otherwise.
- Under 80 words unless the user asked for a breakdown.
- One idea, then one question. No greetings, no encouragement, no hype, no
  lectures, no em dashes.
- If STATE.decision.reasons is not empty, you may quote those codes as plain
  reasons. You may not invent a reason that is not in the list.

ENDINGS:
- If STATE.decision.confirm_id is set, end with exactly one line:
  CONFIRM: <that id>
- If the verdict is to wait, end with: CONFIRM: WAIT
- If the verdict is a refusal, end with: CONFIRM: NO
- Otherwise end with one short question, or nothing.

LOCKED DOLLAR SLEEVE (when STATE carries a vault draft or vault view):
- The sleeve is Rail-owned tiers, human labels only. Never name assets,
  chains, tokens, wallets, seeds, or providers.
- Four facts first, then the draft: it is a locked dollar retirement sleeve;
  deposits can come out; growth before unlock costs 10% of the growth taken;
  it opens on the STATE unlock date. Then the STATE numbers, nothing else.
""".strip()


def voice_system_prompt() -> str:
    """The base Miriam prompt plus the narration-layer contract."""
    try:
        base = build_system_prompt()
    except Exception:  # noqa: BLE001 - a prompt build must never break a turn
        base = "You are Miriam, a money operator. You are read-only."
    return f"{base}\n\n{VOICE_LAYER_PROMPT}"


def _render_facts(facts: list[dict[str, Any]] | None) -> str:
    """Remembered facts about the user, capped so nothing drowns the numbers."""
    if not facts:
        return ""
    lines = []
    for fact in facts[:8]:
        content = str(fact.get("content") or "").strip()
        if not content:
            continue
        kind = str(fact.get("type") or "fact")
        lines.append(f"- [{kind}] {content}")
    if not lines:
        return ""
    return "WHAT YOU REMEMBER ABOUT THIS USER:\n" + "\n".join(lines)


def build_voice_messages(
    state: HandlerState,
    utterance: str = "",
    facts: list[dict[str, Any]] | None = None,
    *,
    breakdown: bool = False,
) -> list[ChatMessage]:
    """The messages for one narration call.

    STATE is the payload. There is no second source of numbers, which is what
    makes "only wording changes when the model changes" true.
    """
    parts = [
        "STATE (the only source of numbers and the only source of the verdict):",
        json.dumps(state.to_dict(), indent=2, sort_keys=True),
    ]
    if utterance:
        parts.append(f'THE USER SAID:\n"{utterance}"')
    remembered = _render_facts(facts)
    if remembered:
        parts.append(remembered)
    if breakdown:
        parts.append("The user asked for the numbers, so a short breakdown is fine.")
    parts.append("Write the one message now.")

    return [
        ChatMessage(role="system", content=voice_system_prompt()),
        ChatMessage(role="user", content="\n\n".join(parts)),
    ]


__all__ = ["VOICE_LAYER_PROMPT", "build_voice_messages", "voice_system_prompt"]
