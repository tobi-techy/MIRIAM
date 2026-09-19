"""The canonical set of tools that move money or mutate backend state.

Every consumer that needs to answer "is this a money tool?" imports from here.
Previously four independently-maintained copies existed and had already drifted:

- ``safety/policy.py`` allowlist
- ``safety/policy.py`` ``_MONEY_ACTIONS`` (audit filter for daily-limit sums)
- ``api/chat.py`` ``_MONEY_TOOLS`` (which tool arguments get audited)
- ``tools/investment_definitions.py`` ``STAGED_CONFIRMATION_TOOLS``

The first three are consolidated here. The automation and scheduled-investment
mutations were gated for approval but were missing from the audit and
daily-limit lists, so their amounts were never recorded. That is the class of
drift this module removes -- ``tests/test_money_tools.py`` asserts the registry
and this file cannot disagree.

``STAGED_CONFIRMATION_TOOLS`` in ``investment_definitions`` is deliberately
*not* folded in: it answers a different question (which Go endpoints answer
with a payload-bound confirmation token that the agent may replay), not "does
this touch money".
"""

from __future__ import annotations

# Every tool that mutates backend state and must therefore be staged behind an
# explicit user confirmation.
#
# Curated rather than derived from the registry on purpose: a newly registered
# mutation cannot silently inherit money-movement privileges just by existing.
# The drift test enforces the other direction -- registering a mutation without
# listing it here fails the build.
MONEY_TOOLS: frozenset[str] = frozenset(
    {
        # Money movement (Go ledger)
        "send_money",
        "transfer_stash_to_spending",
        "transfer_spending_to_stash",
        "pay_bill",
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
        # Glider investment Agent API
        "create_strategy",
        "update_strategy",
        "enroll_strategy",
        "pause_strategy",
        "resume_strategy",
        "rebalance_strategy",
        "buy_asset",
        "sell_asset",
        "set_allocation",
    }
)

# The subset that actually moves money, now or on a schedule. Only these carry
# amounts that count toward the per-transaction and daily transfer caps.
#
# Create/config tools that merely *record* an intention (a saved beneficiary, a
# tracked bill) live in MONEY_TOOLS but not here: their amounts must never be
# summed into "how much did this user transfer today".
TRANSFER_TOOLS: frozenset[str] = frozenset(
    {
        "send_money",
        "transfer_stash_to_spending",
        "transfer_spending_to_stash",
        "pay_bill",
        "buy_asset",
        "sell_asset",
        "enroll_strategy",
        "create_scheduled_investment",
        "create_automation",
    }
)
