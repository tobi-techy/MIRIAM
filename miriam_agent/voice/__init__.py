"""Layer 3 - VOICE. An LLM and a memory. Read-only, and read-only for real.

Voice is called only after Hands assembled STATE and Judgment filled
``STATE.decision``. It narrates; it decides nothing and moves nothing.

Three properties make "read-only" structural rather than aspirational:

* :mod:`~miriam_agent.voice.prompt` gives it the personality prompt plus a
  narration contract, and nothing that authorises a movement.
* :mod:`~miriam_agent.voice.generate` calls the provider with no tools, and
  clamps every figure against STATE before a reply is allowed out.
* :mod:`~miriam_agent.voice.memory` reads facts about the person, never
  balances, and fails open to nothing.

There is no function in this package that writes a balance, calls a rail, or
edits a decision. If Voice could cause a side effect by emitting text, the
design would be wrong; the only thing its text can cause is a confirmation
lookup by id, and an id it did not get from STATE is refused.
"""

from __future__ import annotations

from miriam_agent.voice.generate import (
    MATERIAL_NUMBER,
    WORD_CEILING,
    VoiceMessage,
    clamp_violations,
    deterministic_message,
    extract_confirm,
    speak,
    state_vocabulary,
)
from miriam_agent.voice.memory import read_facts
from miriam_agent.voice.prompt import (
    VOICE_LAYER_PROMPT,
    build_voice_messages,
    voice_system_prompt,
)

__all__ = [
    "MATERIAL_NUMBER",
    "VOICE_LAYER_PROMPT",
    "WORD_CEILING",
    "VoiceMessage",
    "build_voice_messages",
    "clamp_violations",
    "deterministic_message",
    "extract_confirm",
    "read_facts",
    "speak",
    "state_vocabulary",
    "voice_system_prompt",
]
