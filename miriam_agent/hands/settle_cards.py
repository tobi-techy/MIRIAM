"""Layer 1 - HANDS. Face ID confirm-card helpers.

Moved verbatim out of :mod:`miriam_agent.orchestrator` (blob-ratchet split):
best-effort card mint/sync. Cards never fail the turn. Mixed into
``Orchestrator``; the runtime surface is unchanged.
"""

from __future__ import annotations

import logging

from miriam_agent.hands.audit import Receipt
from miriam_agent.hands.ledger import Challenge, Ledger

logger = logging.getLogger(__name__)


class CardSettlementMixin:
    """Best-effort Face ID card helpers."""

    async def _maybe_mint_card(
        self, ledger: Ledger, confirm_id: str, *, channel: str, thread_id: str
    ) -> str:
        """Mint a Go Face ID card for a staged challenge. Best-effort.

        Returns the Go action id, or "" when no card applies. Never fails the
        turn: cards disabled, a non-allowlisted channel, an unmappable action
        (lock/unlock, onramp/offramp, invest enroll), or any mint error all
        fall back to the existing narration path with the chat confirm intact.
        """
        from miriam_agent.confirm_cards import client as cards_client
        from miriam_agent.confirm_cards.mapping import card_for_action

        if not self.cards_enabled:
            return ""
        if (channel or "").strip().lower() not in [
            allowed.strip().lower() for allowed in self.card_channels
        ]:
            return ""
        challenge = ledger.challenges.get(confirm_id)
        if challenge is None:
            return ""
        spec = card_for_action(challenge)
        if spec is None:
            return ""
        payload = dict(spec.payload)
        payload.setdefault("thread_id", thread_id)
        payload.setdefault("space_id", thread_id)
        if not self.go_token:
            return ""
        try:
            minted = await cards_client.mint_card(
                self.go_token,
                go_action=spec.go_action,
                payload=payload,
                thread_id=thread_id,
                title=spec.title,
                subtitle=spec.subtitle,
            )
        except Exception as exc:  # noqa: BLE001 - cards never fail the turn
            logger.warning("confirm card mint failed, text flow continues: %s", exc)
            return ""
        challenge.card_action_id = minted.action_id
        challenge.card_state = "pending"
        challenge.channel = channel
        ledger.challenges[confirm_id] = challenge
        try:
            await self.store.save(ledger)
        except Exception as exc:  # noqa: BLE001 - the challenge stands; no card
            logger.warning("confirm card join not persisted: %s", exc)
            return ""
        return minted.action_id

    async def _sync_card_terminal(
        self, challenge: Challenge, receipt: Receipt | None
    ) -> None:
        """Tell Go the ending of a card challenge settled on this side.

        Chat tap and settle endpoint converge here: first-writer-wins on the
        consumed flag decides who executes, and the winner marks the live
        card so the transcript shows the same ending. Best-effort: executed
        maps to completed, rejected to failed; anything still in flight
        (queued invest/funding) leaves the card as Go set it.
        """
        action_id = (challenge.card_action_id or "").strip()
        if not action_id:
            return
        if receipt is None:
            return
        if receipt.status == "executed":
            state = "completed"
        elif receipt.status == "rejected":
            state = "failed"
        else:
            return
        summary = (receipt.detail or "")[:300]
        await self._mark_card_state(action_id, state, summary)
        challenge.card_state = state

    async def _mark_card_state(
        self, action_id: str, state: str, summary: str = ""
    ) -> None:
        """Best-effort Go card sync. Never fails the turn."""
        from miriam_agent.config.settings import get_settings
        from miriam_agent.confirm_cards import client as cards_client

        try:
            service_key = get_settings().RAIL_SERVICE_KEY
        except Exception:  # noqa: BLE001 - unconfigured means no sync
            return
        if not service_key:
            return
        try:
            await cards_client.mark_card_terminal(
                service_key, action_id, state, summary
            )
        except Exception as exc:  # noqa: BLE001 - card sync never fails settle
            logger.warning("confirm card mark failed (settle stands): %s", exc)
