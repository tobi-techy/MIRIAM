"""Move a finished guest interview onto the Rail account it just created.

iMessage onboarding runs as a synthetic guest (``guest:{platform}:{sender}``)
until Go links the phone. The next text arrives as the real user id, which
has no interview, so Miriam greets them again. This copies the guest record
onto that account. A real user's row is never treated as a guest.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from miriam_agent.onboarding.state import (
    STAGE_COMPLETE,
    OnboardingState,
    OnboardingStateStore,
    get_onboarding_state_store,
)

logger = logging.getLogger(__name__)

_GUEST_EMAIL_SUFFIX = "@miriam.invalid"
_PLATFORM_CONVERSATION = re.compile(r"^platform:([A-Za-z0-9_]+):(.+)$")
_E164 = re.compile(r"\+[1-9]\d{7,14}")


def guest_user_id(platform: str, sender_id: str) -> str:
    """Same id Go mints in ``guestSyntheticID`` (UUID v5 over the OID namespace)."""
    name = f"guest:{(platform or '').strip().lower()}:{(sender_id or '').strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


def platform_sender(conversation_id: str | None) -> tuple[str, str] | None:
    """Pull ``(platform, e164)`` out of ``platform:{platform}:{thread}``.

    The thread id is ``any;-;+234…``, sometimes with a ``:{user}`` fork suffix.
    The phone is what Go hashed into the guest id.
    """
    match = _PLATFORM_CONVERSATION.match((conversation_id or "").strip())
    if match is None:
        return None
    phone = None
    for found in _E164.finditer(match.group(2)):
        phone = found.group(0)
    if not phone:
        return None
    return match.group(1).lower(), phone


def is_synthetic_guest(user: Any) -> bool:
    email = str(getattr(user, "email", "") or "").lower()
    username = str(getattr(user, "username", "") or "").lower()
    return email.endswith(_GUEST_EMAIL_SUFFIX) or username.startswith("guest_")


def should_replace(real: OnboardingState | None, guest: OnboardingState | None) -> bool:
    """The completed guest interview wins over an empty or restarted real one.

    After signup the real id often holds a fresh greeting (or a name parsed
    out of "yeah, walk me through funding"). That record is the bug, not a
    second interview.
    """
    if guest is None:
        return False
    if real is None:
        return True
    return guest.stage == STAGE_COMPLETE and real.stage != STAGE_COMPLETE


async def transfer_onboarding_state(
    from_user_id: str,
    to_user_id: str,
    store: OnboardingStateStore | None = None,
) -> bool:
    """Copy onboarding Redis state from the guest id onto the real user."""
    if not from_user_id or not to_user_id or from_user_id == to_user_id:
        return False
    store = store or get_onboarding_state_store()
    guest = await store.get_state(from_user_id)
    real = await store.get_state(to_user_id)
    if not should_replace(real, guest):
        return False
    await store.save_state(to_user_id, guest)  # type: ignore[arg-type]
    await store.clear(from_user_id)
    logger.info(
        "moved onboarding state from guest %s onto %s (stage %s)",
        from_user_id,
        to_user_id,
        guest.stage if guest is not None else "",
    )
    return True


async def _load_user(memory_store: Any, user_id: str) -> Any:
    loader = getattr(memory_store, "get_user", None)
    if loader is not None:
        return await loader(user_id)
    if getattr(memory_store, "_session", None) is None:
        return None
    from miriam_agent.database.models import User

    async with memory_store._session() as session:
        return await session.get(User, user_id)


async def adopt_guest_interview(
    memory_store: Any,
    user: Any,
    conversation_id: str | None,
    *,
    state_store: OnboardingStateStore | None = None,
) -> bool:
    """Best-effort handoff for a linked user texting on a guest thread.

    Failures are logged and swallowed. A missing interview must not fail the
    chat turn that is trying to answer the person.
    """
    try:
        parsed = platform_sender(conversation_id)
        if parsed is None:
            return False
        user_id = str(getattr(user, "id", "") or "")
        if not user_id or is_synthetic_guest(user):
            return False
        guest_id = guest_user_id(*parsed)
        if guest_id == user_id:
            return False
        row = await _load_user(memory_store, guest_id)
        if row is not None and not is_synthetic_guest(row):
            logger.warning(
                "refusing guest handoff onto %s; %s is not a guest row",
                user_id,
                guest_id,
            )
            return False
        moved = await transfer_onboarding_state(guest_id, user_id, state_store)
        # History follows the interview once. Later texts find no guest state
        # and do not write on every turn.
        if moved:
            merge = getattr(memory_store, "merge_user_data", None)
            if merge is not None:
                await merge(guest_id, user_id)
        return moved
    except Exception:
        logger.warning("guest interview handoff failed (non-blocking)", exc_info=True)
        return False
