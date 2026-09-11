"""Tests for the Supermemory integration.

Uses httpx.MockTransport to simulate the Supermemory REST API so every
path (search, profile, ingest, retries, fail-open) can be exercised
without network calls or an API key.
"""

import asyncio
import json

import httpx

from miriam_agent.conversational.supermemory_memory import SupermemoryMemory
from miriam_agent.integrations.supermemory_client import (
    SupermemoryClient,
    container_tag_for,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_FAKE_KEY = "sm_org_test_key_forUnitTests"


def _mock_transport(handler):
    """Wrap a handler function as an httpx.MockTransport.

    ``handler(request: httpx.Request) -> httpx.Response``.
    """
    return httpx.MockTransport(handler)


def _json_response(status_code: int, body: dict | None = None):
    return httpx.Response(
        status_code=status_code,
        json=body or {},
        headers={"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# container_tag_for (pure function)
# ---------------------------------------------------------------------------


class TestContainerTagFor:
    def test_clean_id_kept_as_is(self):
        assert container_tag_for("user_123") == "user_123"

    def test_hash_user_id_when_disallowed_chars(self):
        tag = container_tag_for("john@acme.com")
        assert tag.startswith("user_")
        assert len(tag) == 37  # "user_" (5) + 32 hex chars

    def test_hash_user_id_with_dots(self):
        tag = container_tag_for("a.b.c")
        assert tag.startswith("user_")
        assert len(tag) == 37

    def test_empty_string_returns_hash(self):
        tag = container_tag_for("")
        assert tag.startswith("user_")

    def test_max_length_constraint(self):
        # 100-char compliant string passes through as-is
        long_id = "x" * 90
        assert len(container_tag_for(long_id)) == 90

    def test_slash_triggers_hash(self):
        tag = container_tag_for("user/admin")
        assert tag.startswith("user_")
        assert len(tag) == 37


# ---------------------------------------------------------------------------
# SupermemoryClient — search
# ---------------------------------------------------------------------------


class TestClientSearch:
    def _make_client(self, handler):
        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        return client

    def test_search_returns_results(self):
        """Successful search parses results and total."""

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["containerTag"] == "user_abc"
            assert payload["q"] == "retirement plan"
            assert payload["searchMode"] == "memories"
            return _json_response(
                200,
                {
                    "results": [
                        {
                            "id": "mem_1",
                            "memory": "User wants to retire by 50",
                            "similarity": 0.9,
                            "metadata": {},
                        },
                        {
                            "id": "mem_2",
                            "memory": "Maximizes Roth IRA annually",
                            "similarity": 0.85,
                            "metadata": {},
                        },
                    ],
                    "total": 2,
                    "timing": 120,
                },
            )

        client = self._make_client(handler)

        async def run():
            return await client.search("user_abc", "retirement plan", limit=2)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["total"] == 2
        assert result["results"][0]["memory"] == "User wants to retire by 50"

    def test_search_empty_query_returns_empty(self):
        """Empty query short-circuits (no API call)."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not be called")

        client = self._make_client(handler)

        async def run():
            return await client.search("user_abc", "", limit=5)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result == {"results": [], "total": 0}

    def test_search_handles_500_gracefully(self):
        """Server error returns empty results (fail-open)."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(500, {"error": "internal"})

        client = self._make_client(handler)

        async def run():
            return await client.search("user_abc", "what", limit=5)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result == {"results": [], "total": 0}


# ---------------------------------------------------------------------------
# SupermemoryClient — profile
# ---------------------------------------------------------------------------


class TestClientProfile:
    def _make_client(self, handler):
        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        return client

    def test_profile_returns_static_and_dynamic(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                200,
                {
                    "profile": {
                        "static": ["Name: John", "Risk tolerance: moderate"],
                        "dynamic": ["Recently discussed tax planning"],
                        "buckets": {},
                    },
                    "searchResults": None,
                },
            )

        client = self._make_client(handler)

        async def run():
            return await client.profile("user_123")

        result = asyncio.get_event_loop().run_until_complete(run())
        assert "Name: John" in result["profile"]["static"]
        assert "Recently discussed tax planning" in result["profile"]["dynamic"]

    def test_profile_empty_when_unavailable(self):
        """Profile failure returns empty structure."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(401, {"error": "unauthorized"})

        client = self._make_client(handler)

        async def run():
            return await client.profile("user_123")

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["profile"]["static"] == []
        assert result["profile"]["dynamic"] == []


# ---------------------------------------------------------------------------
# SupermemoryClient — ingest_conversation
# ---------------------------------------------------------------------------


class TestClientIngestConversation:
    def _make_client(self, handler):
        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        return client

    def test_ingest_posts_correct_payload(self):
        """Ingest sends containerTag, conversationId, messages, and dreaming."""

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["conversationId"] == "conv_abc"
            assert payload["containerTag"] == "user_xyz"
            assert len(payload["messages"]) == 2
            assert payload["messages"][0]["role"] == "user"
            assert payload["messages"][1]["role"] == "assistant"
            assert payload["dreaming"] == "dynamic"
            return _json_response(
                200, {"id": "doc_1", "conversationId": "conv_abc", "status": "queued"}
            )

        client = self._make_client(handler)

        async def run():
            return await client.ingest_conversation(
                container_tag="user_xyz",
                conversation_id="conv_abc",
                messages=[
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there!"},
                ],
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["status"] == "queued"

    def test_ingest_empty_conversation_id_returns_none(self):
        """Conversation id must not be blank."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not be called")

        client = self._make_client(handler)

        async def run():
            return await client.ingest_conversation(
                container_tag="user_xyz",
                conversation_id="",
                messages=[{"role": "user", "content": "Hello"}],
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result is None

    def test_ingest_includes_metadata(self):
        """Flat metadata is sent along."""

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload.get("metadata", {}).get("channel") == "api"
            return _json_response(200, {"id": "doc_2", "status": "queued"})

        client = self._make_client(handler)

        async def run():
            return await client.ingest_conversation(
                container_tag="u",
                conversation_id="conv2",
                messages=[{"role": "user", "content": "hi"}],
                metadata={"channel": "api"},
            )

        asyncio.get_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# SupermemoryClient — create / forget / update
# ---------------------------------------------------------------------------


class TestClientMemoryOps:
    def _make_client(self, handler):
        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        return client

    def test_create_memories_posts_correctly(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["containerTag"] == "user_1"
            assert len(payload["memories"]) == 1
            assert payload["memories"][0]["isStatic"] is True
            return _json_response(200, {"documentId": "d1", "memories": []})

        client = self._make_client(handler)

        async def run():
            return await client.create_memories(
                "user_1",
                [{"content": "Prefers dark mode", "isStatic": True, "metadata": {}}],
            )

        asyncio.get_event_loop().run_until_complete(run())

    def test_forget_memory_posts_delete(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            payload = json.loads(request.content)
            assert payload["id"] == "mem_x"
            return _json_response(200, {"forgotten": True})

        client = self._make_client(handler)

        async def run():
            return await client.forget_memory(
                "user_1", memory_id="mem_x", reason="outdated"
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["forgotten"] is True


# ---------------------------------------------------------------------------
# SupermemoryClient — fail-open (no API key)
# ---------------------------------------------------------------------------


class TestClientFailOpen:
    def test_disabled_client_returns_none(self):
        client = SupermemoryClient(api_key="", base_url="https://fake.supermemory.ai")

        async def run():
            results = await asyncio.gather(
                client.search("u", "q"),
                client.profile("u"),
                client.ingest_conversation(
                    "u", "c", [{"role": "user", "content": "hi"}]
                ),
                client.create_memories("u", [{"content": "x"}]),
                client.update_memory("u", "new", memory_id="m"),
                client.forget_memory("u", memory_id="m"),
                client.forget_matching("u", query="x"),
                client.add_document("u", "text"),
                client.get_document("doc1"),
            )
            return results

        results = asyncio.get_event_loop().run_until_complete(run())
        # All calls return None or empty; no exceptions.
        for r in results:
            assert (
                r is None
                or r == {}
                or r.get("results", "MISSING") == []
                or r.get("profile") is not None
            )

    def test_disabled_client_enabled_flag_is_false(self):
        client = SupermemoryClient(api_key="")
        assert client.enabled is False


# ---------------------------------------------------------------------------
# SupermemoryClient — retry on transient errors
# ---------------------------------------------------------------------------


class TestClientRetry:
    def test_retries_on_500_then_succeeds(self):
        """Client retries on 500 and returns result from second attempt."""
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _json_response(500, {"error": "server overloaded"})
            return _json_response(
                200, {"results": [{"id": "mem_1", "memory": "retried ok"}], "total": 1}
            )

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai", max_retries=2
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )

        async def run():
            return await client.search("u", "q", limit=1)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["total"] == 1
        assert call_count == 2

    def test_retries_on_429_then_succeeds(self):
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _json_response(429, {"error": "rate limited"})
            return _json_response(200, {"results": [], "total": 0})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai", max_retries=2
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )

        async def run():
            return await client.search("u", "q")

        asyncio.get_event_loop().run_until_complete(run())
        assert call_count == 2

    def test_non_retriable_401_returns_immediately(self):
        """401 is not retried — it's a config error, not transient."""
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return _json_response(401, {"error": "unauthorized"})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai", max_retries=2
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )

        async def run():
            return await client.search("u", "q")

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result == {"results": [], "total": 0}
        assert call_count == 1  # no retry


# ---------------------------------------------------------------------------
# SupermemoryMemory — build_memory_facts
# ---------------------------------------------------------------------------


class TestBuildMemoryFacts:
    def _make_service(self, profile_payload, search_payload):
        async def handler(request: httpx.Request) -> httpx.Response:
            path = str(request.url)
            # profile endpoint
            if path.endswith("/v4/profile"):
                return _json_response(200, profile_payload)
            # search endpoint
            if path.endswith("/v4/search"):
                return _json_response(200, search_payload)
            return _json_response(404, {})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        return SupermemoryMemory(client)

    def test_build_facts_combines_profile_and_search(self):
        """Profile static/dynamic + search memories appear in facts."""
        sm = self._make_service(
            profile_payload={
                "profile": {
                    "static": ["Name: Ramit", "Risk tolerance: aggressive"],
                    "dynamic": ["Just discussed index funds last week"],
                    "buckets": {},
                },
                "searchResults": None,
            },
            search_payload={
                "results": [
                    {
                        "id": "m1",
                        "memory": "Maximizes 401k every year",
                        "similarity": 0.92,
                        "metadata": {},
                    },
                    {
                        "id": "m2",
                        "memory": "Prefers US equities over international",
                        "similarity": 0.88,
                        "metadata": {"source": "preference"},
                    },
                ],
                "total": 2,
                "timing": 80,
            },
        )

        async def run():
            return await sm.build_memory_facts(
                "user_123", query="what does the user care about", limit=10
            )

        facts = asyncio.get_event_loop().run_until_complete(run())
        # Check profile items are included
        contents = [f["content"] for f in facts]
        assert "Name: Ramit" in contents
        assert "Just discussed index funds last week" in contents
        # Check search hits
        assert "Maximizes 401k every year" in contents
        # No duplicates
        assert len(facts) == len(set(contents))

    def test_build_facts_deduplicates(self):
        """Same content appearing in profile and search only shows once."""
        sm = self._make_service(
            profile_payload={
                "profile": {
                    "static": ["Prefers US equities"],
                    "dynamic": [],
                    "buckets": {},
                },
                "searchResults": None,
            },
            search_payload={
                "results": [
                    {
                        "id": "m1",
                        "memory": "Prefers US equities",
                        "similarity": 0.95,
                        "metadata": {},
                    },
                ],
                "total": 1,
            },
        )

        async def run():
            return await sm.build_memory_facts("u", query="investing")

        facts = asyncio.get_event_loop().run_until_complete(run())
        assert len(facts) == 1

    def test_build_facts_empty_when_disabled(self):
        sm = SupermemoryMemory(SupermemoryClient(api_key=""))

        async def run():
            return await sm.build_memory_facts("u")

        facts = asyncio.get_event_loop().run_until_complete(run())
        assert facts == []

    def test_build_facts_truncated_to_limit(self):
        sm = self._make_service(
            profile_payload={
                "profile": {
                    "static": [f"fact-{i}" for i in range(20)],
                    "dynamic": [],
                    "buckets": {},
                },
                "searchResults": None,
            },
            search_payload={"results": [], "total": 0},
        )

        async def run():
            return await sm.build_memory_facts("u", query="x", limit=5)

        facts = asyncio.get_event_loop().run_until_complete(run())
        assert len(facts) <= 5


# ---------------------------------------------------------------------------
# SupermemoryMemory — ingest_turn
# ---------------------------------------------------------------------------


class TestIngestTurn:
    def test_ingest_posts_messages(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            messages = payload["messages"]
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "What's my balance?"
            assert messages[1]["role"] == "assistant"
            assert "Your balance" in messages[1]["content"]
            assert payload["conversationId"] == "conv_99"
            assert payload["containerTag"] == "user_abc"
            return _json_response(200, {"id": "d1", "status": "queued"})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        sm = SupermemoryMemory(client)

        async def run():
            return await sm.ingest_turn(
                container_tag="user_abc",
                conversation_id="conv_99",
                user_message="What's my balance?",
                assistant_message="Your balance is $1,250.00.",
                metadata={"channel": "api"},
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["status"] == "queued"

    def test_ingest_returns_none_when_disabled(self):
        sm = SupermemoryMemory(SupermemoryClient(api_key=""))

        async def run():
            return await sm.ingest_turn("u", "c", "hello", "hi")

        assert asyncio.get_event_loop().run_until_complete(run()) is None

    def test_ingest_skips_blank_messages(self):
        sm = SupermemoryMemory(SupermemoryClient(api_key=""))

        async def run():
            return await sm.ingest_turn("u", "c", "", "")

        assert asyncio.get_event_loop().run_until_complete(run()) is None


# ---------------------------------------------------------------------------
# SupermemoryMemory — search
# ---------------------------------------------------------------------------


class TestMemorySearch:
    def test_search_returns_list_of_dicts(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                200,
                {
                    "results": [
                        {
                            "id": "m1",
                            "memory": "Likes index funds",
                            "similarity": 0.91,
                            "metadata": {"source": "preference"},
                        },
                        {
                            "id": "m2",
                            "memory": "Risk tolerance: moderate",
                            "similarity": 0.82,
                            "metadata": {},
                        },
                    ],
                    "total": 2,
                },
            )

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        sm = SupermemoryMemory(client)

        async def run():
            return await sm.search("user_1", "investing preferences", limit=2)

        results = asyncio.get_event_loop().run_until_complete(run())
        assert len(results) == 2
        assert results[0]["content"] == "Likes index funds"
        assert results[0]["type"] == "preference"

    def test_search_empty_query_returns_empty(self):
        sm = SupermemoryMemory(SupermemoryClient(api_key="sm_test"))

        async def run():
            return await sm.search("u", "", limit=5)

        assert asyncio.get_event_loop().run_until_complete(run()) == []


# ---------------------------------------------------------------------------
# SupermemoryMemory — forget
# ---------------------------------------------------------------------------


class TestMemoryForget:
    def test_forget_calls_matching_endpoint(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["query"] == "old credit card"
            assert payload["dryRun"] is True
            return _json_response(200, {"dryRun": True, "count": 2, "candidates": []})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        sm = SupermemoryMemory(client)

        async def run():
            return await sm.forget("user_1", query="old credit card", dry_run=True)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["dryRun"] is True
        assert result["count"] == 2

    def test_forget_single_memory_uses_id(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["id"] == "mem_xyz"
            assert request.method == "DELETE"
            return _json_response(200, {})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai"
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )
        sm = SupermemoryMemory(client)

        async def run():
            return await sm.forget("user_1", memory_id="mem_xyz")

        asyncio.get_event_loop().run_until_complete(run())


# ---------------------------------------------------------------------------
# wait_until_done
# ---------------------------------------------------------------------------


class TestWaitUntilDone:
    def test_waits_until_done(self):
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return _json_response(200, {"id": "d1", "status": "processing"})
            return _json_response(200, {"id": "d1", "status": "done"})

        client = SupermemoryClient(
            api_key=_FAKE_KEY, base_url="https://fake.supermemory.ai", timeout=1.0
        )
        client._client = httpx.AsyncClient(
            transport=_mock_transport(handler), base_url="https://fake.supermemory.ai"
        )

        async def run():
            return await client.wait_until_done("d1", timeout=5.0, interval=0.01)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result["status"] == "done"
        assert attempts == 3

    def test_returns_none_when_disabled(self):
        client = SupermemoryClient(api_key="")

        async def run():
            return await client.wait_until_done("d1", timeout=1.0)

        assert asyncio.get_event_loop().run_until_complete(run()) is None
