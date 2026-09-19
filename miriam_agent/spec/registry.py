"""Canonical spec registry with deterministic rule enforcement.

This module provides machine-checkable access to Miriam's behavioral specification.
All citations in the codebase point to this canonical source.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import hashlib
import json
import pathlib

# --- Section registry ---
SECTIONS = {
    # Cited sections from existing code
    5: {
        "title": "High-Information Questions",
        "topic": "spec §5 asks concrete categories, not feelings",
        "rule_ids": ["§5/§53"],
        "enforce_by": ["R2"],
    },
    6: {
        "title": "Money Moment & Golden Rule",
        "topic": "spec §6 defines the money moment opener and THE GOLDEN RULE",
        "rule_ids": ["§6"],
        "enforce_by": ["R1"],
    },
    7: {
        "title": "Goal Read (Reserved Keys)",
        "topic": "spec §7: structured read of the goal, lifted from reserved fact keys",
        "rule_ids": ["§7"],
        "enforce_by": ["§22/§29"],
    },
    8: {
        "title": "Behavior, Not Character",
        "topic": "spec §8: talk about BEHAVIOR, never character (no identity attacks)",
        "rule_ids": ["§8"],
        "enforce_by": ["R5"],
    },
    9: {
        "title": "Never a Therapist",
        "topic": "spec §9: never invite emotional processing back onto the user",
        "rule_ids": ["§9"],
        "enforce_by": ["R12"],
    },
    12: {
        "title": "Ranked Hypotheses & Directness",
        "topic": "spec §12: ranked hypothesis engine, relationship depth + pattern confidence drives directness",
        "rule_ids": ["§12"],
        "enforce_by": ["§29/§12"],
    },
    14: {
        "title": "Reply Length",
        "topic": "spec §14: short, paragraph-bounded replies (120 words conversational, 220 for plan presentation)",
        "rule_ids": ["§14"],
        "enforce_by": ["R1"],
    },
    15: {
        "title": "Ask vs Tell",
        "topic": "spec §15: default to ASK while cause unclear, but only high-information questions; when evidence points one way, TELL",
        "rule_ids": ["§15"],
        "enforce_by": ["R2"],
    },
    16: {
        "title": "No Generic Praise",
        "topic": "spec §16: no generic praise / generic reassurance lines",
        "rule_ids": ["§16"],
        "enforce_by": ["R3"],
    },
    17: {
        "title": "No Generic Advice",
        "topic": "spec §17: no one-size-fits-all generic advice frames",
        "rule_ids": ["§17"],
        "enforce_by": ["R4"],
    },
    21: {
        "title": "One Insight Per Plan",
        "topic": "spec §21: every plan carries one deterministic financial_insight object",
        "rule_ids": ["§21"],
        "enforce_by": ["§27"],
    },
    22: {
        "title": "Money Scripts Internal",
        "topic": "spec §22: money scripts watched silently, never named to the user",
        "rule_ids": ["§22"],
        "enforce_by": ["§22/§29"],
    },
    27: {
        "title": "Success = Real Insight, Not Completion",
        "topic": "spec §27: success is measured as a real insight, not a completed form",
        "rule_ids": ["§27"],
        "enforce_by": ["§27"],
    },
    29: {
        "title": "Conversation State",
        "topic": "spec §29: deterministic, backend-owned read of where the conversation is",
        "rule_ids": ["§29"],
        "enforce_by": ["§29/§12"],
    },
    30: {
        "title": "Never Invent Numbers",
        "topic": "spec §30: never invent numbers, balances, or account status; must come from system of record or approved tool",
        "rule_ids": ["§30"],
        "enforce_by": ["R10"],
    },
    33: {
        "title": "One Question at a Time",
        "topic": "spec §33: one question at a time (at most one '?')",
        "rule_ids": ["§33"],
        "enforce_by": ["R2"],
    },
    35: {
        "title": "No Corporate Boilerplate",
        "topic": "spec §35: no corporate/airship boilerplate",
        "rule_ids": ["§35"],
        "enforce_by": ["R6"],
    },
    50: {
        "title": "Aha Detection",
        "topic": "spec §50: aha moment detection (spec v1.1 §50-§51)",
        "rule_ids": ["§50"],
        "enforce_by": ["§50/§51"],
    },
    51: {
        "title": "Aha Action",
        "topic": "spec §51: surfacing an 'aha' in the user's reply is the whole action",
        "rule_ids": ["§51"],
        "enforce_by": ["§50/§51"],
    },
    52: {
        "title": "Personality Regressions",
        "topic": "spec §52: personality regressions, wired end to end",
        "rule_ids": ["§52"],
        "enforce_by": ["§52/§53"],
    },
    53: {
        "title": "Tested Conversation",
        "topic": "spec §53: spec v1.1 §53 regression: mirror then concrete, not feelings",
        "rule_ids": ["§53"],
        "enforce_by": ["R11/R12"],
    },
    64: {
        "title": "Observation Ends Without Question",
        "topic": "spec §64: a useful observation can end without a question",
        "rule_ids": ["§64"],
        "enforce_by": ["R1"],
    },
    # Additional sections defined in the spec
    1: {"title": "Identity & Role", "topic": "spec §1: Miriam is a financial intelligence agent", "rule_ids": ["§1"], "enforce_by": []},
    2: {"title": "Conversation Behavior", "topic": "spec §2: rules for answering, questioning, and interaction", "rule_ids": ["§2"], "enforce_by": []},
    3: {"title": "Financial Reasoning", "topic": "spec §3: reasoning order and problem identification", "rule_ids": ["§3"], "enforce_by": []},
    4: {"title": "Truth & Uncertainty", "topic": "spec §4: truth rules and uncertainty handling", "rule_ids": ["§4"], "enforce_by": []},
    5: {"title": "Actions & Confirmation", "topic": "spec §5: transaction classes and confirmation policy", "rule_ids": ["§5"], "enforce_by": []},
    6: {"title": "Personality", "topic": "spec §6: core personality traits and anti-patterns", "rule_ids": ["§6"], "enforce_by": []},
    7: {"title": "Regional Behavior", "topic": "spec §7: currency handling and localization", "rule_ids": ["§7"], "enforce_by": []},
}

# --- Rule registry ---
RULES = {
    "R1": {"§N": [14], "enforced_by": ["§14"]},
    "R2": {"§N": [15, 33], "enforced_by": ["§15", "§33"]},
    "R3": {"§N": [16], "enforced_by": ["§16"]},
    "R4": {"§N": [17], "enforced_by": ["§17"]},
    "R5": {"§N": [8], "enforced_by": ["§8"]},
    "R6": {"§N": [35], "enforced_by": ["§35"]},
    "R7": {"§N": [], "enforced_by": ["§22"]},  # tapping rule
    "R8": {"§N": [22], "enforced_by": ["§22"]},
    "R9": {"§N": [], "enforced_by": []},  # no bullet lists in conversational replies
    "R10": {"§N": [30], "enforced_by": ["§30"]},
    "R11": {"§N": [6, 53], "enforced_by": ["§6", "§53"]},
    "R12": {"§N": [9], "enforced_by": ["§9"]},
}

# --- Transaction classes ---
TRANSACTION_CLASSES = {
    "transfer": {
        "description": "Movement of funds between accounts",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "send_money": {
        "description": "Sending money to a person or merchant",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "withdrawal": {
        "description": "Taking funds out of an account",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "investment": {
        "description": "Purchase of investment products or assets",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "card_creation": {
        "description": "Creating new payment cards",
        "confirmation_required": True,
        "risk_level": "medium",
        "enforcement_site": "safety_policy",
    },
    "card_funding": {
        "description": "Funding payment cards",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "bill_payment": {
        "description": "Payment of bills or merchant transactions",
        "confirmation_required": True,
        "risk_level": "high",
        "enforcement_site": "safety_policy",
    },
    "automation_change": {
        "description": "Changes to automated financial rules or schedules",
        "confirmation_required": True,
        "risk_level": "medium",
        "enforcement_site": "safety_policy",
    },
}

# Per-tool transaction classes. Every tool that mutates backend state is named
# here so this registry and the live tool registry can be compared directly
# (tests/test_miriam_spec.py). Previously only the conceptual classes above
# existed, and ``transfer_stash_to_spending``, ``pay_bill`` and the
# record-keeping mutations had no coverage at all -- the drift the tests caught.
_TOOL_TRANSACTION_CLASSES: dict[str, tuple[str, str]] = {
    "send_money": ("Sending money to a person or merchant", "high"),
    "transfer_stash_to_spending": (
        "Moving funds from the yield stash to the spend wallet",
        "high",
    ),
    "transfer_spending_to_stash": (
        "Moving funds from the spend wallet to the yield stash",
        "high",
    ),
    "pay_bill": ("Paying a bill (airtime, data, electricity, cable)", "high"),
    "create_automation": ("Creating a lasting money rule", "high"),
    "update_automation": ("Pausing, resuming or renaming an automation", "medium"),
    "delete_automation": ("Deleting an automation", "medium"),
    "create_scheduled_investment": ("Creating a recurring investment", "high"),
    "pause_scheduled_investment": ("Pausing a recurring investment", "medium"),
    "resume_scheduled_investment": ("Resuming a recurring investment", "medium"),
    "create_obligation": ("Recording a tracked bill or debt", "medium"),
    "mark_obligation_paid": ("Marking a tracked obligation paid", "medium"),
    "save_bill_beneficiary": ("Saving a bill-payment beneficiary", "medium"),
    "create_strategy": ("Creating an investment strategy", "high"),
    "update_strategy": ("Publishing a new strategy version", "high"),
    "enroll_strategy": ("Enrolling funds into a strategy", "high"),
    "pause_strategy": ("Pausing a strategy", "medium"),
    "resume_strategy": ("Resuming a strategy", "medium"),
    "rebalance_strategy": ("Rebalancing a strategy", "high"),
    "buy_asset": ("Buying an asset toward a target allocation", "high"),
    "sell_asset": ("Selling an asset toward a target allocation", "high"),
    "set_allocation": ("Setting a portfolio target allocation", "high"),
}

TRANSACTION_CLASSES.update(
    {
        name: {
            "description": description,
            "confirmation_required": True,
            "risk_level": risk_level,
            "enforcement_site": "safety_policy",
        }
        for name, (description, risk_level) in _TOOL_TRANSACTION_CLASSES.items()
    }
)

# --- Reasoning order ---
REASONING_ORDER = [
    "INCOME",
    "CASH_FLOW",
    "ESSENTIALS",
    "SAFETY",
    "DEBT",
    "GOALS",
    "INVESTING",
    "OPTIMIZATION",
]

# --- Anti-patterns ---
ANTI_PATTERNS = {
    "AP-001": {
        "name": "Parroting",
        "description": "Lifting long clause from previous user message and appending low-information therapist tail",
        "violates": ["R11"],
        "fix": "Substitute real information for empty tail",
    },
    "AP-002": {
        "name": "Therapist Mode",
        "description": "Questions pushing emotional processing back onto the user",
        "violates": ["R12"],
        "fix": "Name the pattern plainly, don't run therapy session",
    },
    "AP-003": {
        "name": "Identity Attack",
        "description": "Framing the person, not the behavior",
        "violates": ["R5"],
        "fix": "Frame the tough stuff as what they do, not who they are",
    },
    "AP-004": {
        "name": "Generic Praise",
        "description": "\"great question,\" \"love that,\" \"awesome\"",
        "violates": ["R3"],
        "fix": "Never open with praise - answer directly",
    },
    "AP-005": {
        "name": "Generic Advice",
        "description": "\"you should budget,\" \"you need to save\"",
        "violates": ["R4"],
        "fix": "Earn specifics by listening, then reflect their own words",
    },
}

# --- Spec hash calculation ---
SPEC_HASH = hashlib.sha256(
    json.dumps(
        {
            "version": "1.2",
            "sections": SECTIONS,
            "rules": RULES,
            "transaction_classes": TRANSACTION_CLASSES,
            "reasoning_order": REASONING_ORDER,
            "anti_patterns": ANTI_PATTERNS,
        },
        sort_keys=True,
    ).encode()
).hexdigest()

# --- Loading utilities ---
def load_markdown() -> str:
    """Load the spec markdown from its path."""
    spec_path = pathlib.Path(__file__).parent / ".." / "docs" / "miriam_spec" / "miriam_spec_v1.2.md"
    return spec_path.read_text(encoding="utf-8")
def sections() -> dict[int, dict[str, Any]]:
    """Return the SECTIONS mapping."""
    return SECTIONS
def content_hash() -> str:
    """Return the spec content hash."""
    return SPEC_HASH
