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
    []) must actually detect repeated large transfers."""
    from miriam_agent.safety.policy import SafetyPolicy

    policy = SafetyPolicy()
    threshold = next(
        p["threshold"]
        for p in policy.suspicious_patterns
        if p["name"] == "rapid_large_transactions"
    )
    large_activities = [
        {"type": "transfer", "amount": threshold, "beneficiary": "x"}
        for _ in range(int(threshold))
    ]
    monkeypatch.setattr(
        policy, "_get_recent_activities", _fake_activities(large_activities)
    )

    detected = _run(
        policy._detect_suspicious_patterns({}, "u-1", financial_profile=None)
    )
    assert detected is True


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
    async def _fake(user_id, timeframe_hours=24):
        return activities

    return _fake
