"""Conversational financial onboarding for Miriam.

A poll-driven, adaptive money interview that runs entirely in the Python brain.
The Go backend renders Miriam's questions as iMessage polls, forwards the user's
votes (or free text), and hands over bank statements as scanned summaries. The
interview collects the Financial State Model signals, builds a deterministic
financial plan (diagnostic state + prioritized steps + standing rules), asks for
explicit consent, and persists everything as long-term memory facts so Miriam
acts on it without re-asking.

The answers and plan are intentionally storeable as ``memory_entries`` of type
``onboarding`` so a future self-training pass can mine all users' aggregated
answers.
"""
