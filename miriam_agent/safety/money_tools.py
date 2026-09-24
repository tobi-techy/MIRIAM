"""The tool names that must never face a model, and never face a rail caller.

One purpose, and one answer to a question several modules used to answer
separately: "is this tool name one of the money family?"

The answer is used in two places, both of which are structural:

* ``tools/definitions.build_tool_registry`` removes every one of them from the
  live registry, so the agent loop has nothing to find, and
* ``agents/agent_loop`` refuses one outright if a model still asks for it.

``miriam_agent.hands`` is the only thing in the process that moves money. It is
reached through ``orchestrator.py``, and it does not go through a tool at all,
so the correct number of money tools in the registry is zero.

This list is curated rather than derived from the registry on purpose. A newly
registered mutation must not inherit the ability to be reachable, and a name
that a well-meaning future change might invent for the same job has to fail a
test rather than quietly reopen the rail.
"""

from __future__ import annotations

# Names that move money today, plus the names a future change might reach for.
MONEY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Money movement
        "send_money",
        "transfer",
        "transfer_money",
        "send",
        "move_money",
        "transfer_stash_to_spending",
        "transfer_spending_to_stash",
        "pay_bill",
        "withdraw",
        "redeem",
        # Sleeves and the income split
        "split",
        "split_income",
        "auto_split",
        "lock",
        "unlock",
        "lock_sleeve",
        "unlock_sleeve",
        "set_sleeve",
        "credit_sleeve",
        "debit_sleeve",
        "change_track",
        "set_track",
        "update_track",
        # Lasting behaviour whose future runs move money unattended
        "create_automation",
        "update_automation",
        "delete_automation",
        "create_scheduled_investment",
        "pause_scheduled_investment",
        "resume_scheduled_investment",
        # Record-keeping that still mutates backend state
        "create_obligation",
        "mark_obligation_paid",
        "save_bill_beneficiary",
        # Investments
        "invest",
        "create_strategy",
        "update_strategy",
        "enroll_strategy",
        "pause_strategy",
        "resume_strategy",
        "rebalance_strategy",
        "buy_asset",
        "sell_asset",
        "set_allocation",
        # Glider enrollment writes. These names must never become registry
        # tools: enrollment runs in hands/invest.py after a confirm_id tap,
        # never from a chat-turn tool call.
        "glider_prepare_enroll",
        "glider_complete_enroll",
        "glider_prepare_deposit_intent",
        # NGN <-> crypto funding verbs. Reads are registry tools; these
        # mutations run in hands/funding.py after a confirm_id tap (Paj OTP
        # for onramp; staged envelope + rail://authorize for offramp) and
        # must never become registry tools.
        "buy_crypto",
        "sell_crypto",
        "sell_crypto_now",
        "create_onramp",
        "verify_paj_otp",
        "initiate_paj_session",
        "create_offramp",
    }
)


def is_money_tool(name: str) -> bool:
    """Whether a tool name belongs to the family that must never face a model."""
    return name in MONEY_TOOL_NAMES


__all__ = ["MONEY_TOOL_NAMES", "is_money_tool"]
