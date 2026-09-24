"""Layer 1 - HANDS. Settle legs for sleeve and rule actions.

Moved verbatim out of :mod:`miriam_agent.orchestrator` (blob-ratchet split):
``_handle_confirm_invest/order/rebalance/save_rule`` and the shared
``_order_calls`` binder. Mixed into ``Orchestrator`` as legs; the runtime
surface is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from miriam_agent.hands.audit import AuditLog, Receipt
from miriam_agent.hands.invest import INVEST_SLEEVE, prepare_allocate
from miriam_agent.hands.ledger import Ledger, LedgerStore
from miriam_agent.hands.limits import Policy
from miriam_agent.hands.orders import prepare_order, prepare_set_allocation
from miriam_agent.hands.rebalance import (
    prepare_pause,
    prepare_rebalance,
    prepare_resume,
)
from miriam_agent.hands.save_rule import select_save_automation
from miriam_agent.hands.settlement import (
    Event,
    TurnResult,
    challenge_owner_ok,
    maybe_reopen_challenge,
)
from miriam_agent.hands.state import (
    Execution,
    HandlerState,
    ProposedAction,
    build_state,
)
from miriam_agent.judgment.schema import Decision


class LegSettlementMixin:
    """Settle legs for sleeve and rule actions.

    Mixed into :class:`miriam_agent.orchestrator.Orchestrator`, which
    provides the runtime surface declared below (store, policy, clock,
    voice, and the Go-call binders). Declared here so the type gate
    checks the legs against the contract instead of `Any`.
    """

    store: LedgerStore
    policy: Policy
    go_token: str | None
    clock: Callable[[], datetime]

    async def _speak(
        self, state: HandlerState, *, utterance: str = ""
    ) -> str | None: ...
    def _execution_from(self, receipt: Receipt | None) -> Execution | None: ...

    # NOTE: _sync_card_terminal is intentionally NOT declared here. The real
    # implementation lives on CardSettlementMixin (later in the MRO), and a
    # stub def on this class would shadow it at runtime and silently disable
    # the Go card-terminal sync.

    async def _challenge_mismatch(
        self,
        ledger: Ledger,
        challenge: Any,
        *,
        receipt_action: str,
        proposed_type: str,
    ) -> TurnResult:
        """Burn a cross-user confirm_id with a mismatch refusal, never execute."""
        now = self.clock()
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge
        receipt = Receipt(
            id=f"rcpt_rejected_{challenge.id}",
            at=now,
            status="rejected",
            action=receipt_action,
            currency=ledger.currency,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            decision_id=challenge.decision_id,
            reasons=["CHALLENGE_MISMATCH"],
            detail="that confirmation was issued to a different account",
        )
        ledger.remember_receipt(receipt)
        await self.store.save(ledger)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=ProposedAction(
                type=proposed_type,  # type: ignore[arg-type]
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                source="user",
            ),
            decision={
                "id": "",
                "next_mode": "ask",
                "action_choice": "deny",
                "reasons": ["CHALLENGE_MISMATCH"],
            },
            now=now,
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            audit=[],
        )

    async def _handle_confirm_invest(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle an invest tap: run Glider stage 1 and return the sign card."""
        from miriam_agent.hands.ledger import money as _money

        audit = AuditLog()
        now = self.clock()
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._challenge_mismatch(
                ledger,
                challenge,
                receipt_action="invest_prepare",
                proposed_type="invest",
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type="invest",
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        calls = self._invest_calls()
        if calls is None:
            receipt = Receipt(
                id=f"rcpt_rejected_{challenge.id}",
                at=now,
                status="rejected",
                action="invest_prepare",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                reasons=["GLIDER_UNREACHABLE"],
                detail="no Go host token on this path; nothing moved",
            )
            ledger.remember_receipt(receipt)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                proposed_action=action,
                decision=decision.model_dump(mode="json"),
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=receipt,
                confirm_id=challenge.id,
                audit=audit.rows,
            )

        owner_address = (event.wallet_address or "").strip() or None
        receipt, card, ledger = await prepare_allocate(
            store=self.store,
            ledger=ledger,
            user_id=event.user_id,
            token=self.go_token or "",
            amount=_money(challenge.amount),
            source="savings" if challenge.sleeve == INVEST_SLEEVE else challenge.sleeve,
            decision_id=challenge.decision_id,
            owner_address=owner_address,
            list_strategies=calls["list_strategies"],
            get_owner=calls["get_owner"],
            prepare_call=calls["prepare_call"],
            at=now,
        )
        # prepare_allocate only saves on paths that change provider state, so
        # persist here as well: the consumed challenge and the rejected receipt
        # must survive the request even when nothing moved. Transient
        # provider failures reopen the challenge so a retap can retry.
        maybe_reopen_challenge(ledger, challenge, receipt)
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            card=card,
            audit=audit.rows,
        )

    # -- shared steps -----------------------------------------------------

    async def _handle_confirm_order(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle an order/set-allocation tap: stage + replay, return the card."""
        from miriam_agent.hands.ledger import money as _money

        audit = AuditLog()
        now = self.clock()
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._challenge_mismatch(
                ledger,
                challenge,
                receipt_action="order_prepare",
                proposed_type=challenge.action,
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type=challenge.action,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        calls = self._order_calls()
        if calls is None:
            receipt = Receipt(
                id=f"rcpt_rejected_{challenge.id}",
                at=now,
                status="rejected",
                action="order_prepare",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                reasons=["GLIDER_UNREACHABLE"],
                detail="no Go host token on this path; nothing moved",
            )
            ledger.remember_receipt(receipt)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                proposed_action=action,
                decision=decision.model_dump(mode="json"),
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=receipt,
                confirm_id=challenge.id,
                audit=audit.rows,
            )

        meta = getattr(challenge, "meta", {}) or {}
        if challenge.action == "set_allocation":
            import json as _json

            try:
                legs = _json.loads(meta.get("legs", "[]"))
            except Exception:
                legs = []
            receipt, card, ledger = await prepare_set_allocation(
                store=self.store,
                ledger=ledger,
                user_id=event.user_id,
                token=self.go_token or "",
                strategy_id=meta.get("strategy_id", ""),
                legs=legs if isinstance(legs, list) else [],
                decision_id=challenge.decision_id,
                allocation_call=calls["allocation_call"],
                at=now,
            )
        else:
            side = meta.get("side", "buy")
            symbol = challenge.counterparty or meta.get("symbol", "")
            receipt, card, ledger = await prepare_order(
                store=self.store,
                ledger=ledger,
                user_id=event.user_id,
                token=self.go_token or "",
                side=side,
                symbol=symbol,
                amount_usd=_money(challenge.amount),
                decision_id=challenge.decision_id,
                list_strategies=calls["list_strategies"],
                search_assets=calls["search_assets"],
                order_call=calls["order_call"],
                at=now,
            )
        maybe_reopen_challenge(ledger, challenge, receipt)
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        await self._sync_card_terminal(challenge, receipt)
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            card=card,
            audit=audit.rows,
        )

    async def _handle_confirm_rebalance(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle a rebalance/pause/resume tap: immediate, no staging."""
        audit = AuditLog()
        now = self.clock()
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._challenge_mismatch(
                ledger,
                challenge,
                receipt_action=f"{challenge.action}_prepare",
                proposed_type=challenge.action,
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type=challenge.action,
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )
        calls = self._order_calls()
        if calls is None:
            receipt = Receipt(
                id=f"rcpt_rejected_{challenge.id}",
                at=now,
                status="rejected",
                action=f"{challenge.action}_prepare",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                reasons=["GLIDER_UNREACHABLE"],
                detail="no Go host token on this path; nothing moved",
            )
            ledger.remember_receipt(receipt)
            await self.store.save(ledger)
            state = build_state(
                ledger=ledger,
                policy=self.policy,
                proposed_action=action,
                decision=decision.model_dump(mode="json"),
                now=now,
            )
            return TurnResult(
                state=state,
                narration=await self._speak(state),
                decision=state.decision,
                receipt=receipt,
                confirm_id=challenge.id,
                audit=audit.rows,
            )

        meta = getattr(challenge, "meta", {}) or {}
        strategy_id = meta.get("strategy_id") or meta.get("strategy") or ""
        if challenge.action == "rebalance":
            receipt, card, ledger = await prepare_rebalance(
                store=self.store,
                ledger=ledger,
                user_id=event.user_id,
                token=self.go_token or "",
                strategy_id=strategy_id,
                reason=None,
                rebalance_call=calls["rebalance_call"],
                at=now,
            )
        elif challenge.action == "pause":
            receipt, card, ledger = await prepare_pause(
                store=self.store,
                ledger=ledger,
                user_id=event.user_id,
                token=self.go_token or "",
                strategy_id=strategy_id,
                pause_call=calls["pause_call"],
                at=now,
            )
        else:
            receipt, card, ledger = await prepare_resume(
                store=self.store,
                ledger=ledger,
                user_id=event.user_id,
                token=self.go_token or "",
                strategy_id=strategy_id,
                resume_call=calls["resume_call"],
                at=now,
            )
        maybe_reopen_challenge(ledger, challenge, receipt)
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        await self._sync_card_terminal(challenge, receipt)
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            card=card,
            audit=audit.rows,
        )

    async def _handle_confirm_save_rule(
        self, ledger: Ledger, event: Event, challenge: Any
    ) -> TurnResult:
        """Settle a save-rule tap: resolve the automation, update it, receipt.

        The percentage/amount comes from the challenge binding (derived from
        the user's own words at stage time), never from a card payload. The
        automation is resolved at settle time: an explicit id in the binding
        wins, else the single automation, else a save/sweep-looking one —
        otherwise the turn is rejected and nothing moves.
        """
        from miriam_agent.hands.audit import AuditLog as _AuditLog

        audit = _AuditLog()
        now = self.clock()
        if not challenge_owner_ok(challenge, ledger, event):
            return await self._challenge_mismatch(
                ledger,
                challenge,
                receipt_action="save_rule_update",
                proposed_type="save_rule",
            )
        challenge.status = "consumed"
        ledger.challenges[challenge.id] = challenge

        action = ProposedAction(
            type="save_rule",
            amount=challenge.amount,
            counterparty=challenge.counterparty,
            sleeve=challenge.sleeve,
            source="user",
        )
        decision = Decision(
            id=challenge.decision_id,
            at=now,
            next_mode="act",
            action_choice="allow",
            suggested_amount=challenge.amount,
            reasons=list(challenge.reasons),
            confirm_id=challenge.id,
        )

        def _reject(reasons: list[str], detail: str) -> Receipt:
            return Receipt(
                id=f"rcpt_rejected_{challenge.id}",
                at=now,
                status="rejected",
                action="save_rule_update",
                currency=ledger.currency,
                amount=challenge.amount,
                counterparty=challenge.counterparty,
                sleeve=challenge.sleeve,
                decision_id=challenge.decision_id,
                reasons=reasons,
                detail=detail,
            )

        meta = getattr(challenge, "meta", {}) or {}
        percentage = (meta.get("percentage") or "").strip()
        amount = (meta.get("amount") or "").strip()
        token = self.go_token
        receipt: Receipt | None = None
        if token and (percentage or amount):
            from miriam_agent.integrations.go_client import get_go_client

            try:
                automations = await get_go_client().list_automations(token)
            except Exception as exc:  # noqa: BLE001 - rail errors are receipts
                automations = []
                receipt = _reject(
                    ["AUTOMATIONS_UNREACHABLE"],
                    f"could not list save rules: {exc}; nothing moved",
                )
            if receipt is None:
                auto = select_save_automation(
                    automations if isinstance(automations, list) else [],
                    automation_id=(meta.get("automation_id") or "").strip(),
                )
                if auto is None:
                    receipt = _reject(
                        ["NO_SAVE_RULE"],
                        "no save rule to update; nothing moved",
                    )
                else:
                    action_cfg: dict[str, Any] = {}
                    if percentage:
                        action_cfg["percentage"] = percentage
                    else:
                        action_cfg["amount"] = amount
                    try:
                        await get_go_client().update_automation(
                            token,
                            str(auto.get("id") or auto.get("automation_id") or ""),
                            {"action_config": action_cfg},
                        )
                    except Exception as exc:  # noqa: BLE001 - rail errors are receipts
                        receipt = _reject(
                            ["SAVE_RULE_UPDATE_FAILED"],
                            f"the save rule did not update: {exc}; nothing moved",
                        )
                    else:
                        if percentage:
                            detail = f"Save rule now parks {percentage}% of inflow"
                        else:
                            detail = f"Save rule updated ({amount} per inflow)"
                        receipt = Receipt(
                            id=f"rcpt_save_rule_{challenge.id}",
                            at=now,
                            status="executed",
                            action="save_rule_update",
                            currency=ledger.currency,
                            amount=challenge.amount,
                            counterparty=challenge.counterparty,
                            sleeve=challenge.sleeve,
                            decision_id=challenge.decision_id,
                            idempotency_key=f"save-rule:{challenge.id}",
                            confirm_id=challenge.id,
                            detail=detail,
                        )
        if receipt is None:
            receipt = _reject(
                ["SAVE_RULE_UNBOUND"],
                "that confirmation does not bind a save rule; nothing moved",
            )
        maybe_reopen_challenge(ledger, challenge, receipt)
        ledger.remember_receipt(receipt)
        await self.store.save(ledger)
        execution = self._execution_from(receipt)
        state = build_state(
            ledger=ledger,
            policy=self.policy,
            proposed_action=action,
            decision=decision.model_dump(mode="json"),
            execution=execution,
            now=self.clock(),
        )
        await self._sync_card_terminal(challenge, receipt)
        return TurnResult(
            state=state,
            narration=await self._speak(state),
            decision=state.decision,
            receipt=receipt,
            confirm_id=challenge.id,
            audit=audit.rows,
        )

    def _order_calls(self) -> dict[str, Any] | None:
        """Bound Go-host calls for the order leg, or None (fail closed)."""
        token = self.go_token
        if not token:
            return None
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()

        async def list_strategies() -> dict[str, Any]:
            return await client.list_investment_strategies(token, status="active")

        async def search_assets() -> dict[str, Any]:
            return await client.list_investment_assets(token, query=None, limit=100)

        async def order_call(payload: dict[str, Any]) -> dict[str, Any]:
            tok = payload.pop("confirmation_token", None)
            return await client.create_investment_order(
                token, payload, confirmation_token=tok
            )

        async def allocation_call(payload: dict[str, Any]) -> dict[str, Any]:
            tok = payload.pop("confirmation_token", None)
            return await client.set_investment_allocation(
                token, payload, confirmation_token=tok
            )

        async def rebalance_call(
            strategy_id: str, reason: str | None = None
        ) -> dict[str, Any]:
            return await client.rebalance_investment_strategy(
                token, strategy_id, reason=reason
            )

        async def pause_call(strategy_id: str) -> dict[str, Any]:
            return await client.pause_investment_strategy(token, strategy_id)

        async def resume_call(strategy_id: str) -> dict[str, Any]:
            return await client.resume_investment_strategy(token, strategy_id)

        return {
            "list_strategies": list_strategies,
            "search_assets": search_assets,
            "order_call": order_call,
            "allocation_call": allocation_call,
            "rebalance_call": rebalance_call,
            "pause_call": pause_call,
            "resume_call": resume_call,
        }
