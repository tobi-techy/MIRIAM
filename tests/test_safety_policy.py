"""Regression tests for miriam_agent.safety.policy.SafetyPolicy.

Before this pass, `_get_recent_activities` always returned `[]`, which
silently disabled every pattern check (multiple large transfers, new
beneficiary, round-number transfers) and made real daily-limit enforcement
impossible (there was nothing to sum). These tests prove those checks are
now reachable and behave correctly, without requiring a real database --
`_get_recent_activities` is monkeypatched directly so the tests stay fast
and hermetic (the real audit-log wiring is exercised by running the app
end-to-end, not by unit tests).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_limits_are_sourced_from_settings_not_hardcoded():
    """Regression: limits used to be a separate hardcoded dict, disconnected
    from config/settings.py."""
    from miriam_agent.config.settings import get_settings
    from miriam_agent.safety.policy import SafetyPolicy

    settings = get_settings()
    policy = SafetyPolicy()

    assert policy.money_movement_limits["daily_limit"] == settings.MAX_DAILY_TRANSFER
    assert (
        policy.money_movement_limits["transaction_limit"]
        == settings.MAX_TRANSACTION_AMOUNT
    )


def test_normal_single_transfer_is_allowed(monkeypatch):
    """Regression guard: the new daily-limit math must not block ordinary,
    low-volume activity."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    monkeypatch.setattr(policy, "_get_recent_activities", _fake_activities([]))

    allowed = _run(
        policy._check_limits({"amount": 50.0}, "u-1", financial_profile=None)
    )
    assert allowed is True


def test_daily_limit_blocks_when_todays_total_would_be_exceeded(monkeypatch):
    """A user who already moved close to their daily cap today must be
    blocked from pushing over it, even though this single transaction is
    well under the per-transaction limit."""
    from miriam_agent.config.settings import get_settings
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    daily_limit = get_settings().MAX_DAILY_TRANSFER
    already_spent_today = daily_limit - 10.0  # $10 of headroom left today
    monkeypatch.setattr(
        policy,
        "_get_recent_activities",
        _fake_activities([{"type": "transfer", "amount": already_spent_today}]),
    )

    allowed = _run(
        policy._check_limits({"amount": 100.0}, "u-1", financial_profile=None)
    )
    assert allowed is False


def test_multiple_large_transfers_pattern_now_fires(monkeypatch):
    """Regression: with real recent activity, the rapid-large-transactions
    pattern (previously permanently dead since recent_activities was always
    []) must actually detect repeated large transfers.

    The count and the size are separate knobs: the pattern needs
    ``threshold`` transfers that are each at or above ``amount_floor``.
    """
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    pattern = next(
        p for p in policy.suspicious_patterns if p["name"] == "rapid_large_transactions"
    )
    count = int(pattern["threshold"])
    floor = float(pattern["amount_floor"])
    large_activities = [
        {"type": "transfer", "amount": floor, "beneficiary": "x"} for _ in range(count)
    ]
    monkeypatch.setattr(
        policy, "_get_recent_activities", _fake_activities(large_activities)
    )

    detected = _run(
        policy._detect_suspicious_patterns({}, "u-1", financial_profile=None)
    )
    assert detected is True

    # ...and it is the one pattern that may refuse an action outright.
    blocking = _run(policy._blocking_pattern_fired({}, "u-1"))
    assert blocking is True


def test_ordinary_small_transfers_never_trip_the_velocity_pattern(monkeypatch):
    """Regression: ``threshold`` doubled as the per-transfer size floor, so
    three ordinary $3 sends read as "rapid large transactions" and denied the
    user's next send. Small transfers must never be suspicious on size alone.
    """
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    small_activities = [
        {"type": "transfer", "amount": 3.0, "beneficiary": "x"} for _ in range(20)
    ]
    monkeypatch.setattr(
        policy, "_get_recent_activities", _fake_activities(small_activities)
    )

    assert _run(policy._blocking_pattern_fired({}, "u-1")) is False
    assert _run(policy._is_user_suspicious("u-1")) is False


def test_new_beneficiary_pattern_is_advisory_not_blocking(monkeypatch):
    """A first-time $3,000 transfer to a new recipient is what the pattern is
    for -- it must be noticed, not hard-denied (that left no way to ever
    establish a history with the recipient)."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    monkeypatch.setattr(policy, "_get_recent_activities", _fake_activities([]))

    fired = _run(
        policy._fired_patterns({"to": "new@rail.io", "amount": 3000.0}, "u-1")
    )
    assert "unusual_recipients" in fired
    assert _run(policy._blocking_pattern_fired({"to": "new@rail.io", "amount": 3000.0}, "u-1")) is False


def test_no_recent_activity_means_no_pattern_detected(monkeypatch):
    """Regression guard: an empty (or unreachable-audit-log) history must
    never itself be treated as suspicious."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    monkeypatch.setattr(policy, "_get_recent_activities", _fake_activities([]))

    detected = _run(
        policy._detect_suspicious_patterns({}, "u-1", financial_profile=None)
    )
    assert detected is False


def test_pay_bill_allowed_and_uses_amount_ngn_for_limits(monkeypatch):
    """The bill-pay door must be an approved money tool, and its NGN face
    value must count toward per-transaction/daily caps like any other move."""
    from miriam_agent.config.settings import get_settings
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    daily_limit = get_settings().MAX_DAILY_TRANSFER
    already_paid = daily_limit - 10.0
    monkeypatch.setattr(
        policy,
        "_get_recent_activities",
        _fake_activities([{"type": "transfer", "amount": already_paid}]),
    )

    allowed = _run(policy._is_action_allowed("pay_bill"))
    assert allowed is True

    # A large payment that pushes over the daily cap is blocked even though
    # its amount lives in amount_ngn, not amount.
    blocked = _run(
        policy._check_limits({"amount_ngn": 100.0}, "u-1", financial_profile=None)
    )
    assert blocked is False

    # A small payment stays under the cap.
    monkeypatch.setattr(policy, "_get_recent_activities", _fake_activities([]))
    ok = _run(policy._check_limits({"amount_ngn": 50.0}, "u-1", financial_profile=None))
    assert ok is True


def _fake_activities(activities):
    async def _fake(user_id, timeframe_hours=24, strict=False):
        return activities

    return _fake


# -------------------------------------------------------------------------
# Approval workflow + strict fail-closed limits
# -------------------------------------------------------------------------


def test_approval_workflow_thresholds_are_numeric_and_settings_sourced():
    """Regression: required_for_large_amounts used to be the boolean True and
    auto_approve was hardcoded (100.0) while settings.AUTO_APPROVE_THRESHOLD
    went unused."""
    from miriam_agent.config.settings import get_settings
    from miriam_agent.safety.policy import SafetyPolicy

    settings = get_settings()
    policy = SafetyPolicy()

    assert (
        policy.approval_workflow["required_for_large_amounts"]
        == settings.APPROVAL_REQUIRED_ABOVE
    )
    assert (
        policy.approval_workflow["auto_approve_below_threshold"]
        == settings.AUTO_APPROVE_THRESHOLD
    )
    assert isinstance(
        policy.approval_workflow["required_for_large_amounts"], (int, float)
    )
    assert not isinstance(
        policy.approval_workflow["required_for_large_amounts"], bool
    )


def test_readonly_tool_never_requires_approval():
    """Read-only tools used to be flagged "requires approval" because the
    default tool risk (0.5) + user baseline (0.4) pinned them at critical."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()

    requires = _run(
        policy._requires_approval("get_balance", {}, "u-1", None, "critical")
    )
    assert requires is False


def test_money_tool_requires_approval_from_amount_ngn():
    """Bill pay carries its face value in amount_ngn; a large payment must be
    flagged even with risk_level forced low."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()

    requires = _run(
        policy._requires_approval(
            "pay_bill", {"amount_ngn": 5000.0}, "u-1", None, "low"
        )
    )
    assert requires is True


def test_validate_action_denies_unapproved_money_and_allows_approved(monkeypatch):
    """The approval policy used to only log and still let the action through;
    now an action that requires approval is denied without an approved flag."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    monkeypatch.setattr(policy, "_get_recent_activities", _fake_activities([]))

    denied = _run(
        policy.validate_action(
            tool_name="send_money",
            arguments={"to": "alice@rail.io", "amount": 10.0},
            user_id="u-1",
            financial_profile=None,
        )
    )
    assert denied is False

    allowed = _run(
        policy.validate_action(
            tool_name="send_money",
            arguments={"to": "alice@rail.io", "amount": 10.0},
            user_id="u-1",
            financial_profile=None,
            approved=True,
        )
    )
    assert allowed is True

    # Read-only tools stay allowed without approval.
    read_ok = _run(
        policy.validate_action(
            tool_name="get_balance",
            arguments={},
            user_id="u-1",
            financial_profile=None,
        )
    )
    assert read_ok is True


def test_daily_limit_fails_closed_when_activity_lookup_fails(monkeypatch):
    """If the audit trail cannot be read, the daily-limit check must DENY
    (fail-closed) rather than treat the outage as "no activity today"."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()

    async def _boom(user_id, timeframe_hours=24, strict=False):
        raise RuntimeError("audit db down")

    monkeypatch.setattr(policy, "_get_recent_activities", _boom)

    allowed = _run(policy._check_limits({"amount": 10.0}, "u-1", None))
    assert allowed is False


def test_new_beneficiary_pattern_fires_for_send_money_to_field():
    """send_money carries the recipient in `to`, not destination_account; the
    pattern check must see it or the safeguard never fires on real sends."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    pattern = next(
        p for p in policy.suspicious_patterns if p["name"] == "unusual_recipients"
    )
    detected = _run(
        policy._check_new_beneficiary_unusual_amount(
            pattern,
            {"to": "new@rail.io", "amount": 3000.0},
            [],
        )
    )
    assert detected is True
