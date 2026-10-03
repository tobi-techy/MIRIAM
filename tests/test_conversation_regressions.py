"""Regressions for the iMessage conversation bugs reported 2026-09-30.

Three defects were visible in a real onboarding + funding conversation:

* the onboarding read-back was emitted twice, verbatim;
* the plan copy said "$700 is left" and then "nothing is left to spend freely";
* "Locked in." replayed the entire plan instead of a short receipt.

See the diagnosis notes: the read-back guard checked ``facts_confirmed`` but not
``awaiting_fact_confirm``, so a redelivered turn re-sent the same sentence.
"""
from __future__ import annotations

from test_onboarding import _run, _service, _user, FakeProvider


def _heard(turn) -> str:
    """The whole delivered text: a read-back can be split across bubbles."""
    return " ".join([turn.response, *turn.messages]).strip()


def _walk_to_read_back(monkeypatch):
    """Drive the scripted interview up to (but not through) the fact read-back."""
    user = _user()
    provider = FakeProvider()
    service, states, _, _ = _service(monkeypatch, provider)

    _run(service.handle_turn(user, message="Hey"))
    _run(service.handle_turn(user, message="Tobiloba"))
    _run(service.handle_turn(user, message="$50 or less"))
    _run(service.handle_turn(user, message="Irregular"))
    readback = _run(service.handle_turn(user, message="$30"))
    return user, service, states, readback


def test_read_back_is_not_repeated_verbatim_on_redelivery(monkeypatch):
    """A second identical turn must not re-send the identical read-back sentence."""
    user, service, states, first = _walk_to_read_back(monkeypatch)

    assert "Is that right?" in _heard(first)
    assert states.data["u-1"]["awaiting_fact_confirm"] is True

    # The same inbound delivered again (retry / redelivery) before the user
    # confirms. Previously this produced the identical sentence a second time.
    second = _run(service.handle_turn(user, message="$30"))

    assert _heard(second) != _heard(first)
    assert "Is that right?" not in _heard(second)

    # Still unconfirmed, so the user can still confirm or change a number.
    assert states.data["u-1"]["awaiting_fact_confirm"] is True
    assert states.data["u-1"]["facts_confirmed"] is False


def test_plan_copy_does_not_contradict_itself():
    """The leftover is pre-allocation; the copy must not read as a contradiction."""
    from miriam_agent.onboarding.money_bridge import plain_month_lines

    plan = {
        "currency": "USD",
        "monthly_take_home": 1000.0,
        "cashflow": {"fixed": "300.0", "savings": "700.00", "guilt_free": "0.00"},
        "buffer": {"target_amount": "1800.00", "target_months": 6},
    }
    text = " ".join(plain_month_lines(plan))

    assert "Before the plan does anything, that leaves $700." in text
    assert "After the plan, nothing is left to spend freely this month." in text
    # The old ambiguous wording is gone: it read as "you have $700 / you have $0".
    assert "That leaves $700." not in text


def test_completion_receipt_does_not_replay_the_whole_plan():
    """After "Locked in." the receipt carries the numbers, not the plan again."""
    from miriam_agent.onboarding.completion import automated_completion_text
    from miriam_agent.onboarding.money_bridge import build_money_plan_dict
    from miriam_agent.onboarding.state import OnboardingState

    state = OnboardingState(
        {
            "learned": {"income": "I earn 500k every month", "fixed": "rent and food take 350k"},
            "goal": "build buffer",
            "interview_turns": 4,
            "stage": "plan_consent",
        }
    )
    state.money_plan = build_money_plan_dict(state)
    receipt = automated_completion_text(state)

    assert "Locked in." in receipt
    # Still tells the user the two numbers that matter...
    assert "350" in receipt and "150" in receipt
    # ...but does not re-emit the plan they just read.
    assert "does not fill it" not in receipt
    assert "Nothing goes to stocks yet." not in receipt
    assert "The buffer should" not in receipt
