"""Per-person memory cohesion.

The isolation boundary was never the bug: ``container_tag_for`` has always put
one person in one container. The bug was that one person produced several
*disconnected documents* -- a new document per web session, a separate one for
onboarding, and nothing at all from the Spectrum gateway -- so the memory graph
read as several "Tobiloba" clusters instead of one connected memory.

These tests pin the fix: a stable per-person, per-channel scope, container
grounding via ``entityContext``, and every channel actually writing.
"""

import asyncio
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from miriam_agent.conversational import supermemory_memory as sm_module
from miriam_agent.conversational.supermemory_memory import SupermemoryMemory
from miriam_agent.integrations.supermemory_client import (
    SupermemoryClient,
    container_tag_for,
    conversation_scope_for,
    display_name_for,
    person_entity_context,
)

_FAKE_KEY = "sm_org_test_key_forUnitTests"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client(handler) -> SupermemoryClient:
    client = SupermemoryClient(
        api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://fake.supermemory.ai",
    )
    return client


def _ok(body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json=body or {},
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Pure helpers: the scope and the grounding
# ---------------------------------------------------------------------------


class TestConversationScope:
    def test_scope_is_stable_for_the_same_person_and_channel(self):
        assert conversation_scope_for("user_1", "web") == conversation_scope_for(
            "user_1", "web"
        )

    def test_scope_differs_by_channel_and_by_person(self):
        assert conversation_scope_for("u", "web") != conversation_scope_for(
            "u", "imessage"
        )
        assert conversation_scope_for("u1", "web") != conversation_scope_for(
            "u2", "web"
        )

    def test_scope_symbol_is_a_valid_document_id(self):
        scope = conversation_scope_for(
            "00000000-0000-5000-8000-000000000001", "WhatsApp"
        )
        assert scope == "miriam:whatsapp:00000000-0000-5000-8000-000000000001"
        assert len(scope) <= 255

    def test_scope_sanitizes_a_hostile_channel(self):
        scope = conversation_scope_for("u", "im/message ")
        assert scope.startswith("miriam:im-message:")
        assert "/" not in scope

    def test_blank_channel_defaults_to_web(self):
        assert conversation_scope_for("u", "") == "miriam:web:u"


class TestDisplayName:
    def test_it_prefers_the_full_name(self):
        assert display_name_for("Tobiloba Adeyemi", "tobi") == "Tobiloba Adeyemi"

    def test_it_falls_back_to_username(self):
        assert display_name_for("", "tobi") == "tobi"

    def test_token_placeholders_are_not_a_name(self):
        # get_current_user fills missing claims with these; they must never
        # become the label on a person's whole memory space.
        assert display_name_for("Unknown User", "unknown") is None
        assert display_name_for(None, None) is None


class TestPersonEntityContext:
    def test_it_names_the_person(self):
        context = person_entity_context("user_1", "Tobiloba")
        assert "Tobiloba" in context
        assert "user_1" in context
        assert len(context) <= 1500

    def test_it_falls_back_without_a_name(self):
        context = person_entity_context("user_1")
        assert "this person" in context

    def test_hashed_ids_stay_inside_the_container_boundary(self):
        # A tag that had to be hashed still has to resolve to one person.
        assert conversation_scope_for("john@acme.com", "web").startswith(
            f"miriam:web:{container_tag_for('john@acme.com')}"
        )


# ---------------------------------------------------------------------------
# Client: container settings
# ---------------------------------------------------------------------------


class TestUpdateContainerSettings:
    def test_patches_the_container_with_name_and_entity_context(self):
        seen: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["payload"] = json.loads(request.content)
            return _ok({"containerTag": "user_1"})

        client = _client(handler)
        result = _run(
            client.update_container_settings(
                "user_1", name="Tobiloba", entity_context="about Tobiloba"
            )
        )

        assert seen["method"] == "PATCH"
        assert seen["path"] == "/v3/container-tags/user_1"
        assert seen["payload"] == {
            "name": "Tobiloba",
            "entityContext": "about Tobiloba",
        }
        assert result == {"containerTag": "user_1"}

    def test_no_settings_means_no_call(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("nothing to update, nothing to call")

        client = _client(handler)
        assert _run(client.update_container_settings("user_1")) is None


# ---------------------------------------------------------------------------
# Memory service: grounding, notes
# ---------------------------------------------------------------------------


class TestEnsureContainer:
    def _fresh(self):
        sm_module._CONTAINER_READY.clear()
        sm_module._CONTAINER_RETRY_AT.clear()

    def test_grounds_once_then_memoises(self):
        self._fresh()
        calls: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            return _ok({"containerTag": "user_1"})

        sm = SupermemoryMemory(_client(handler))
        assert _run(sm.ensure_container("user_1", "user_1", name="Tobiloba")) is True
        assert _run(sm.ensure_container("user_1", "user_1", name="Tobiloba")) is True

        assert len(calls) == 1
        assert calls[0]["name"] == "Tobiloba"
        # The whole point: extraction is grounded on whose memory this is.
        assert "Tobiloba" in calls[0]["entityContext"]

    def test_a_failure_cools_down_instead_of_hammering(self):
        self._fresh()
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status_code=500, json={"error": "boom"})

        sm = SupermemoryMemory(_client(handler))
        assert _run(sm.ensure_container("user_1", "user_1")) is False
        attempts_during_failure = calls
        assert _run(sm.ensure_container("user_1", "user_1")) is False
        # Cooldown: the second turn does not sit through another outage.
        assert calls == attempts_during_failure

    def test_disabled_service_never_calls(self):
        self._fresh()
        sm = SupermemoryMemory(SupermemoryClient(api_key=""))
        assert _run(sm.ensure_container("user_1", "user_1")) is False


class TestIngestNote:
    def test_note_appends_a_system_message_to_the_given_document(self):
        seen: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return _ok({"id": "d1", "status": "queued"})

        sm = SupermemoryMemory(_client(handler))
        _run(
            sm.ingest_note(
                container_tag="user_1",
                conversation_id="miriam:onboarding:user_1",
                content="[identity/name] Tobiloba",
                metadata={"channel": "onboarding"},
            )
        )

        assert seen["conversationId"] == "miriam:onboarding:user_1"
        assert seen["containerTag"] == "user_1"
        assert seen["messages"] == [
            {"role": "system", "content": "[identity/name] Tobiloba"}
        ]

    def test_blank_note_is_skipped(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("blank note must not be sent")

        sm = SupermemoryMemory(_client(handler))
        assert _run(sm.ingest_note("u", "c", "   ")) is None


# ---------------------------------------------------------------------------
# Chat path: one stable document per person, per channel
# ---------------------------------------------------------------------------


class RecordingMemory:
    enabled = True

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self.containers: list[tuple] = []

    async def ingest_turn(self, **kwargs: Any) -> dict:
        self.turns.append(kwargs)
        return {"id": "d1"}

    async def ensure_container(self, container_tag, user_id, name=None) -> bool:
        self.containers.append((container_tag, user_id, name))
        return True


class TestChatIngest:
    def test_turns_land_in_a_stable_person_scope(self):
        from miriam_agent.api import chat

        memory = RecordingMemory()
        _run(
            chat._ingest_to_supermemory(
                memory,
                "user_1",
                channel="web",
                user_message="save for a house",
                assistant_message="how much by when?",
                name="Tobiloba",
            )
        )

        turn = memory.turns[0]
        assert turn["conversation_id"] == "miriam:web:user_1"
        assert turn["container_tag"] == "user_1"
        assert turn["metadata"]["channel"] == "web"
        # The person's name grounds the container.
        assert memory.containers[0] == ("user_1", "user_1", "Tobiloba")

    def test_channels_do_not_collide(self):
        from miriam_agent.api import chat

        memory = RecordingMemory()
        for channel in ("web", "onboarding"):
            _run(
                chat._ingest_to_supermemory(
                    memory,
                    "user_1",
                    channel=channel,
                    user_message="hi",
                    assistant_message="hello",
                )
            )
        scopes = {t["conversation_id"] for t in memory.turns}
        assert scopes == {"miriam:web:user_1", "miriam:onboarding:user_1"}


# ---------------------------------------------------------------------------
# Onboarding: settled facts reach the graph
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def store_memory(self, user_id, memory_type, content, metadata=None) -> str:
        self.rows.append((user_id, memory_type, content, metadata))
        return "mem_1"


class FakeSupermemory:
    enabled = True

    def __init__(self) -> None:
        self.notes: list[dict[str, Any]] = []
        self.facts: list[tuple] = []

    async def ingest_note(self, **kwargs: Any) -> dict:
        self.notes.append(kwargs)
        return {"id": "d1"}

    async def remember_person_fact(self, *args: Any, **kwargs: Any) -> dict:
        self.facts.append((args, kwargs))
        return {"id": "m1"}


class TestOnboardingMirror:
    def _service(self, sm):
        from miriam_agent.onboarding.service import OnboardingService

        return OnboardingService(
            FakeStore(),
            state_store=object(),
            trace_store=object(),
            supermemory=sm,
        )

    def test_a_settled_fact_is_appended_to_the_onboarding_document(self):
        sm = FakeSupermemory()
        service = self._service(sm)
        _run(service._remember("user_1", "goal", "goal", "house", is_a_vote=False))

        note = sm.notes[0]
        assert note["container_tag"] == "user_1"
        assert note["conversation_id"] == "miriam:onboarding:user_1"
        assert note["content"] == "[goal/goal] house"

    def test_a_name_is_pinned_as_a_permanent_trait(self):
        sm = FakeSupermemory()
        service = self._service(sm)
        _run(
            service._remember("user_1", "name", "identity", "Tobiloba", is_a_vote=False)
        )

        assert len(sm.facts) == 1
        args, kwargs = sm.facts[0]
        assert "Tobiloba" in args[1]
        assert kwargs["is_static"] is True

    def test_no_service_means_no_network_and_no_crash(self):
        service = self._service(None)
        _run(service._remember("user_1", "goal", "goal", "house", is_a_vote=False))


# ---------------------------------------------------------------------------
# Spectrum gateway: the texting channel writes memory too
# ---------------------------------------------------------------------------


def test_spectrum_turns_reach_the_person_graph(monkeypatch):
    from miriam_agent.api import dependencies, spectrum
    from miriam_agent.api.main import app
    from miriam_agent.database.models import User

    user = User()
    user.id = "u-spec-1"
    user.username = "specter"
    user.email = "s@test.invalid"
    user.full_name = "Tobiloba"
    user.is_active = True
    user.roles = ["verified"]

    class StubStore:
        async def store_interaction(self, **kwargs: Any) -> None:
            return None

    memory = RecordingMemory()
    monkeypatch.setattr(spectrum, "_orchestrator_for", lambda token: object())
    overrides = {
        **app.dependency_overrides,
        dependencies.get_current_user: lambda: user,
        dependencies.get_bearer_token: lambda: "test-token",
        dependencies.get_memory_store: lambda: StubStore(),
        dependencies.get_supermemory_memory_dep: lambda: memory,
    }
    monkeypatch.setattr(app, "dependency_overrides", overrides)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/chat/spectrum",
        json={
            "channel": "imessage",
            "space_id": "sp1",
            "user_id": "u-spec-1",
            "text": "hello there",
        },
    )
    assert response.status_code == 200, response.text[:400]

    assert memory.turns, "the gateway turn never reached the memory graph"
    turn = memory.turns[0]
    assert turn["conversation_id"] == "miriam:imessage:u-spec-1"
    assert turn["container_tag"] == "u-spec-1"
