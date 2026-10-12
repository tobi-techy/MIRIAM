"""Pure scoping helpers for Supermemory container and document ids.

Split out of :mod:`miriam_agent.integrations.supermemory_client` (blob-ratchet)
as a pure move: the containerTag / customId / channel regexes, the placeholder
name set, and the id/scope/grounding functions. The client re-imports every
name, so existing call sites and import paths are unchanged.
"""

import re

# containerTag pattern per the concept page: ^[a-zA-Z0-9_:-]+$ (max 100).
# NOTE: the add-document OpenAPI snippet instead says "hyphens, underscores,
# and dots only", and scoped-key creation allows dots too — the docs disagree
# with each other. Hashing non-matching ids (conservative) always yields a
# tag every validator accepts, so keep the concept-page pattern here.
_CONTAINER_TAG_RE = re.compile(r"^[a-zA-Z0-9_:\-]+$")

# customId pattern per the add-document OpenAPI: max 100 chars, alphanumeric
# with hyphens, underscores, and dots only (no colons — unlike containerTag).
_CUSTOM_ID_RE = re.compile(r"[^a-zA-Z0-9_.\-]")


def container_tag_for(user_id: str) -> str:
    """Sanitize an arbitrary user id into a valid Supermemory containerTag.

    Uses a stable ``user_``-prefixed hash so any upstream id (UUID, email,
    tag, whatever the Go backend issues) maps to a compliant, isolated tag.

    Rules per /docs/concepts/container-tags: ``^[a-zA-Z0-9_:-]+$``,
    max 100 chars. Compliant ids pass through untouched (human-readable,
    deterministic — no lookup needed at query time).
    """
    value = user_id.strip()
    # Keep as-is when already compliant (keeps tags human-readable).
    if value and len(value) <= 100 and _CONTAINER_TAG_RE.match(value):
        return value
    import hashlib

    digest = hashlib.sha256(value.encode()).hexdigest()[:32]
    return f"user_{digest}"


def custom_id_for(value: str) -> str:
    """Sanitize an arbitrary id into a valid Supermemory customId.

    Per the add-document OpenAPI: max 100 chars, alphanumeric plus
    ``-``, ``_``, ``.`` only. Our conversation ids contain colons
    (``onboarding:{user}``), so sending them verbatim as ``customId``
    risks a 400 — map every disallowed char to ``_`` and truncate.
    Falls back to a stable hash when nothing valid remains.
    """
    cleaned = _CUSTOM_ID_RE.sub("_", (value or "").strip())[:100]
    if cleaned.strip("_."):
        return cleaned
    import hashlib

    return f"id_{hashlib.sha256(value.encode()).hexdigest()[:32]}"


_CHANNEL_RE = re.compile(r"[^a-z0-9]+")


def _sanitize_channel(channel: str) -> str:
    token = _CHANNEL_RE.sub("-", (channel or "").strip().lower()).strip("-")
    return token[:24] or "web"


def conversation_scope_for(user_id: str, channel: str = "web") -> str:
    """Stable per-person, per-channel conversation id.

    One person must accumulate one connected memory graph, not a new document
    per chat session. Keying every ingest to a stable
    ``miriam:<channel>:<container>`` scope keeps a channel's turns in a single
    document that Supermemory can diff, and gives ``dreaming: dynamic`` a
    coherent unit to link across, instead of scattering the person across one
    document per session id.

    The container tag (the isolation boundary) is still derived from the user
    id alone, so this remains per-person: two people never share a scope.
    """
    return f"miriam:{_sanitize_channel(channel)}:{container_tag_for(user_id)}"


_PLACEHOLDER_NAMES = {"unknown", "unknown user", "n/a"}


def display_name_for(*candidates: str | None) -> str | None:
    """First real name among the candidates, ignoring token placeholders.

    ``get_current_user`` fills in a missing JWT claim with ``"unknown"`` /
    ``"Unknown User"``. Lifting those into a container name would label a
    person's whole memory space "Unknown User", which is worse than leaving it
    unlabelled, so placeholders are dropped.
    """
    for candidate in candidates:
        value = (candidate or "").strip()
        if value and value.casefold() not in _PLACEHOLDER_NAMES:
            return value
    return None


def person_entity_context(user_id: str, name: str | None = None) -> str:
    """Container-level grounding for a person's memory space.

    Supermemory reads this while processing documents in the container, so it
    is what stops extraction from drifting on unanchored pronouns: without it,
    "I'm saving for a house" is a fact about nobody in particular.
    """
    who = (name or "").strip() or "this person"
    return (
        f"This space holds the long-term memory of {who} (user id {user_id}), "
        f"a person using Miriam, their personal financial assistant. Every fact "
        f"here is about {who} personally; resolve first-person statements "
        f'("I", "me", "my") to {who}.'
    )
