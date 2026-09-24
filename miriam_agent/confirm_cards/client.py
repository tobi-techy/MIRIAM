"""Go confirmation-card HTTP client (Miriam -> Go).

Best-effort by contract: any failure raises :class:`CardUnavailable` and the
caller falls back to the text flow — a card problem never fails the turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from miriam_agent.config.settings import get_settings

logger = logging.getLogger(__name__)

#: Card lifetime asked of Go. Stays the Go default (5 min) and, by
#: construction, well under the 30-min challenge TTL: an expired card renders
#: dead while the challenge is still open, and the same challenge re-mints.
CARD_TTL_SECONDS = 300


class CardUnavailable(Exception):
    """Go could not stage/mark a card. Fall back to the text flow."""


@dataclass(frozen=True)
class MintedCard:
    action_id: str
    confirm_url: str


async def mint_card(
    token: str,
    *,
    go_action: str,
    payload: dict[str, Any],
    thread_id: str = "",
    title: str | None = None,
    subtitle: str | None = None,
    timeout: float = 15.0,
) -> MintedCard:
    """Stage one Go confirmation and dispatch its live card.

    ``token`` is the user's bearer token (same path as GoRail). Creating a
    card never moves money — Face ID success + server accept does.
    """
    base = get_settings().GO_BACKEND_URL.rstrip("/")
    body: dict[str, Any] = {
        "action": go_action,
        "payload": payload,
        "thread_id": thread_id,
        "ttl_seconds": CARD_TTL_SECONDS,
    }
    if title:
        body["title"] = title
    if subtitle:
        body["subtitle"] = subtitle
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/api/v1/confirmations",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        raise CardUnavailable(f"go confirmations unreachable: {exc}") from exc
    if resp.status_code != 201:
        raise CardUnavailable(f"go mint_card: unexpected status {resp.status_code}")
    try:
        data = resp.json()
        action_id = str(data.get("action_id") or "")
        confirm_url = str(data.get("confirm_url") or "")
    except Exception as exc:
        raise CardUnavailable(f"go mint_card: unreadable reply: {exc}") from exc
    if not action_id or not confirm_url:
        raise CardUnavailable("go mint_card: reply missing action_id/confirm_url")
    logger.info("minted go confirm card %s for %s", action_id, go_action)
    return MintedCard(action_id=action_id, confirm_url=confirm_url)


async def mark_card_terminal(
    service_key: str,
    action_id: str,
    state: str,
    result: str = "",
    timeout: float = 10.0,
) -> None:
    """Report a terminal state for a card the chat side settled first.

    Authed by the shared rail secret (X-Rail-Service-Key), not a user JWT:
    the caller is the Miriam backend and the confirm_id join plus the secret
    is the authorization. ``state`` is completed|rejected|failed|expired.
    Raises CardUnavailable on any failure; the caller treats it as a
    best-effort sync, never a turn failure.
    """
    if not service_key:
        raise CardUnavailable("mark_card_terminal: RAIL_SERVICE_KEY is unset")
    base = get_settings().GO_BACKEND_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/api/v1/confirmations/{action_id}/mark",
                json={"state": state, "result": result},
                headers={"X-Rail-Service-Key": service_key},
            )
    except Exception as exc:
        raise CardUnavailable(f"go mark unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise CardUnavailable(f"go mark: unexpected status {resp.status_code}")


__all__ = [
    "CARD_TTL_SECONDS",
    "CardUnavailable",
    "MintedCard",
    "mark_card_terminal",
    "mint_card",
]
