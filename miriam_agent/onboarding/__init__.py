"""Conversational financial onboarding for Miriam.

Miriam runs the whole onboarding as a real conversation: no script, no fixed
question bank. She leads, adapts, and records what she learns as free-form
facts; a deterministic plan builder turns those facts into a Financial State
Model diagnosis (diagnostic state + prioritized steps + standing rules), then
explicit consent turns the plan into living standing rules. Everything is
persisted as long-term memory facts so Miriam acts on it without re-asking.

The Go backend renders Miriam's messages as iMessage polls (optional taps),
forwards votes or free text, and hands over bank statements as scanned
summaries. The facts and plan are intentionally storeable as ``memory_entries``
of type ``onboarding`` so a future self-training pass can mine all users'
aggregated conversations.
"""
