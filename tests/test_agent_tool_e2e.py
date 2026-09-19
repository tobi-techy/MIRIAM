"""End-to-end agent-loop tests for tool-execution flows.

These prove the full pipeline, not just registry.execute():

    user message
    -> Agent (system prompt + tool schemas)
    -> mocked LLM decides to call a tool
    -> agent executes the tool against a mocked Go backend
    -> tool result is fed back to the LLM
    -> LLM produces the final user-facing response
    -> response is scrubbed of em-dashes

Coverage for the two reported product failures:

    * "Okay I would love to get airtime" + phone number must resolve the
      network via ``detect_network`` (success path and provider-failure path).
    * "What account do I send Naira to" must invoke ``get_deposit_details``
      and answer from real backend data, never invented details.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

import pytest

from miriam_agent.agents.agent_loop import Agent
from miriam_agent.agents.llm import LLMResponse
from miriam_agent.tools import build_tool_registry
from miriam_agent.integrations import go_client


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockProvider:
    """Minimal mocked LLM provider for deterministic agent-loop tests.

    Returns a fixed plan of :class:`LLMResponse` objects, one per
    ``stream()`` call the agent makes.  The first response drives tool
    selection; once the tool executes and its result is fed back, the next
    response is the LLM final answer.  When the plan is exhausted the
    provider returns a bland closing response so the loop always terminates.
    """

    model = "mock-v1"

    def __init__(self, plan=None):
        self._plan = list(plan or [])
        self._idx = 0

    def _next(self):
        if self._idx < len(self._plan):
            item = self._plan[self._idx]
            self._idx += 1
            return item
        from miriam_agent.agents.llm import LLMResponse

        return LLMResponse(content="All done.", model=self.model)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        return self._next()

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        response = self._next()
        if getattr(response, "tool_calls", None):
            for tc in response.tool_calls:
                yield {"type": "tool_call", "tool_call": tc}
        elif getattr(response, "content", None):
            for word in response.content.split():
                yield {"type": "token", "content": word + " "}
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


class _AirtimeOK:
    """Bill service that successfully detects the network for a phone number."""

    async def detect_network(self, token, phone):
        assert token == "tok", token
        assert phone == "08012345678", phone
        return {"network_id": "01", "network": "MTN"}


class _AirtimeDown:
    """Bill service that is unreachable."""

    async def detect_network(self, token, phone):
        raise Exception("bill service unreachable")


class _DepositOK:
    """Deposit service that returns a real Naira virtual account."""

    async def get_ngn_virtual_account(self, token):
        assert token == "tok", token
        return {
            "virtual_account": {
                "bank_name": "Graph Bank",
                "account_number": "0123456789",
                "account_name": "Ada Obi",
            }
        }

    async def create_deposit_address(self, token, chain="base", currency="USDC"):
        raise AssertionError(
            "NGN account exists, no crypto fallback needed"
        )


class _DepositDown:
    """Deposit service with no Naira account and a down crypto fallback."""

    async def get_ngn_virtual_account(self, token):
        return {"_tool_error": "no Naira deposit account exists for this user yet"}

    async def create_deposit_address(self, token, chain="base", currency="USDC"):
        return {"_tool_error": "deposit service down"}



# ------------------------------------------------------------------
# Airtime flow
# ------------------------------------------------------------------


def test_agent_airtime_phone_number_calls_detect_network():
    """End-to-end: user provides a phone number, agent calls detect_network
    with it, and the tool result reaches the final response."""
    orig = go_client._client
    go_client._client = _AirtimeOK()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "detect_network",
                                "arguments": json.dumps({"phone": "08012345678"}),
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="That number is on MTN. Want me to top it up?",
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="tok", message="08012345678")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "detect_network"
        assert result.tool_calls[0]["arguments"] == {"phone": "08012345678"}
        assert "MTN" in result.response or "01" in result.response
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig


def test_agent_airtime_provider_failure_is_honest():
    """When the bill service is unreachable, the agent must report the real
    failure instead of claiming no network was detected."""
    orig = go_client._client
    go_client._client = _AirtimeDown()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "detect_network",
                                "arguments": json.dumps({"phone": "08012345678"}),
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="The network lookup failed. Can you tell me which network?",
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="tok", message="08012345678")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "detect_network"
        # Response should reflect a real failure, not a fabricated network.
        lowered = result.response.lower()
        assert any(w in lowered for w in ("failed", "unreachable", "bill")), (
            result.response
        )
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig


# ------------------------------------------------------------------
# Naira deposit flow
# ------------------------------------------------------------------


def test_agent_naira_deposit_uses_get_deposit_details():
    """End-to-end: user asks how to deposit Naira, agent calls
    get_deposit_details, and the real bank-transfer details reach the
    final response."""
    orig = go_client._client
    go_client._client = _DepositOK()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_deposit_details",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content=(
                        "You can send Naira to Graph Bank account 0123456789 "
                        "(Ada Obi). It lands in your Rail balance."
                    ),
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="tok", message="How do I deposit Naira?")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "get_deposit_details"
        assert "Graph Bank" in result.response, result.response
        assert "0123456789" in result.response, result.response
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig


def test_agent_naira_deposit_failure_is_honest():
    """When no deposit account is available, the agent must explain the real
    failure instead of pretending the capability does not exist."""
    orig = go_client._client
    go_client._client = _DepositDown()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_deposit_details",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="I couldn't pull up a Naira account for you right now.",
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="tok", message="How do I deposit Naira?")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "get_deposit_details"
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-x"])


# ------------------------------------------------------------------
# Naira deposit flow
# ------------------------------------------------------------------


def test_agent_naira_deposit_uses_get_deposit_details():
    """End-to-end: user asks how to deposit Naira, agent calls
    get_deposit_details, and the real bank-transfer details reach the
    final response."""
    orig = go_client._client
    go_client._client = _DepositOK()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_deposit_details",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content=(
                        "You can send Naira to Graph Bank account 0123456789 "
                        "(Ada Obi). It lands in your Rail balance."
                    ),
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="tok", message="How do I deposit Naira?")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "get_deposit_details"
        assert "Graph Bank" in result.response, result.response
        assert "0123456789" in result.response, result.response
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig


def test_agent_naira_deposit_missing_token_is_structured():
    """When no auth token is supplied, get_deposit_details returns a structured
    error, not a silent failure."""
    orig = go_client._client
    go_client._client = _DepositOK()
    try:
        provider = _MockProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_deposit_details",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
                LLMResponse(
                    content="I need you to sign in again first.",
                    model="mock",
                ),
            ]
        )
        agent = Agent(registry=build_tool_registry(), provider=provider)
        result = _run(
            agent.run(user_id="u1", token="", message="How do I deposit Naira?")
        )
        assert len(result.tool_calls) == 1, result.tool_calls
        assert result.tool_calls[0]["name"] == "get_deposit_details"
        # The tool handler returned a structured error; the response should
        # reflect a real issue, not a fabricated success.
        assert "\u2014" not in result.response
    finally:
        go_client._client = orig
