"""Guest interview survives the jump from the synthetic sender to the Rail user."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from miriam_agent.onboarding.handoff import (  # noqa: E402
    adopt_guest_interview,
    guest_user_id,
    platform_sender,
    should_replace,
)
from miriam_agent.onboarding.state import (  # noqa: E402
    STAGE_COMPLETE,
    STAGE_GREETING,
    STAGE_INTERVIEW,
    OnboardingState,
    OnboardingStateStore,
)


def test_guest_id_matches_go_sha1():
    # uuid.NewSHA1(NameSpaceOID, "guest:imessage:+2349164904178")
    assert (
        guest_user_id("imessage", "+2349164904178")
        == "f17c08b7-9d0a-5b0c-ba44-2f94f0659f16"
    )


def test_platform_sender_reads_phone_from_thread_id():
    assert platform_sender("platform:imessage:any;-;+2349164904178") == (
        "imessage",
        "+2349164904178",
    )
    assert platform_sender(
        "platform:imessage:any;-;+2349164904178:a28c1e1a-3e6d-4a3d-9fec-8186396cc478"
    ) == ("imessage", "+2349164904178")
    assert platform_sender("conv_web") is None


def test_completed_guest_interview_replaces_a_restarted_greeting():
    guest = OnboardingState({"stage": STAGE_COMPLETE, "name": "Tobiloba"})
    restarted = OnboardingState(
        {"stage": STAGE_INTERVIEW, "name": "Yeah I", "learned": {}}
    )
    assert should_replace(None, guest)
    assert should_replace(restarted, guest)
    assert should_replace(
        OnboardingState({"stage": STAGE_GREETING}), guest
    )
    assert not should_replace(
        OnboardingState({"stage": STAGE_COMPLETE, "name": "Tobiloba"}), guest
    )


class _User:
    def __init__(self, uid: str, email: str = "", username: str = ""):
        self.id = uid
        self.email = email
        self.username = username


class _Memory:
    def __init__(self, guest):
        self.guest = guest
        self.merged = None

    async def get_user(self, user_id: str):
        if self.guest is not None and user_id == self.guest.id:
            return self.guest
        return None

    async def merge_user_data(self, from_id: str, to_id: str):
        self.merged = (from_id, to_id)
        return {"conversations": 1}


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_adopt_moves_completed_guest_state_onto_the_linked_user():
    store = OnboardingStateStore(redis_url="")
    guest_id = guest_user_id("imessage", "+2349164904178")
    guest_state = OnboardingState(
        {"stage": STAGE_COMPLETE, "name": "Tobiloba", "money_ready": True}
    )
    real_id = "a28c1e1a-3e6d-4a3d-9fec-8186396cc478"
    _run(store.save_state(guest_id, guest_state))
    _run(
        store.save_state(
            real_id, OnboardingState({"stage": STAGE_GREETING})
        )
    )
    memory = _Memory(_User(guest_id, email=f"guest_{guest_id[:8]}@miriam.invalid", username="guest_abc"))
    moved = _run(
        adopt_guest_interview(
            memory,
            _User(real_id, email="omotadetobiloba@gmail.com", username="tobi"),
            "platform:imessage:any;-;+2349164904178",
            state_store=store,
        )
    )
    assert moved is True
    assert memory.merged == (guest_id, real_id)
    adopted = _run(store.get_state(real_id))
    assert adopted is not None and adopted.stage == STAGE_COMPLETE
    assert adopted.name == "Tobiloba"
    assert _run(store.get_state(guest_id)) is None


def test_adopt_refuses_a_real_users_row():
    store = OnboardingStateStore(redis_url="")
    guest_id = guest_user_id("imessage", "+2349164904178")
    _run(
        store.save_state(
            guest_id, OnboardingState({"stage": STAGE_COMPLETE, "name": "Tobiloba"})
        )
    )
    memory = _Memory(_User(guest_id, email="someone@gmail.com", username="someone"))
    moved = _run(
        adopt_guest_interview(
            memory,
            _User("real-user", email="me@gmail.com"),
            "platform:imessage:any;-;+2349164904178",
            state_store=store,
        )
    )
    assert moved is False
    assert memory.merged is None
    assert _run(store.get_state(guest_id)) is not None


def test_second_text_does_not_merge_again():
    store = OnboardingStateStore(redis_url="")
    guest_id = guest_user_id("imessage", "+15550001111")
    memory = _Memory(
        _User(guest_id, email="guest_x@miriam.invalid", username="guest_x")
    )
    _run(
        store.save_state(
            guest_id, OnboardingState({"stage": STAGE_COMPLETE, "name": "Ada"})
        )
    )
    user = _User("real", email="ada@gmail.com")
    conv = "platform:imessage:any;-;+15550001111"
    assert _run(adopt_guest_interview(memory, user, conv, state_store=store)) is True
    memory.merged = None
    assert _run(adopt_guest_interview(memory, user, conv, state_store=store)) is False
    assert memory.merged is None
