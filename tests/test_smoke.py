"""Smoke tests for Miriam Financial Agent (no external services required).

Mocks LLM provider and memory store to test the full agent loop and
API integration locally. Verifies:
  - All modules import
  - Tool registry builds correctly
  - Agent loop routes auto-execute vs staged confirmation
  - Streaming produces token / action_required / done events
  - API endpoints respond correctly
"""

import asyncio
import json
import os
import sys

import pytest

# Skip entire suite if the app won't boot (missing deps in CI)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


# -----------------------------------------------------------------------
# Mock LLM provider
# -----------------------------------------------------------------------


class MockProvider:
    model = "mock-v1"

    def __init__(self, response_plan=None):
        self._plan = response_plan or []
        self._call_idx = 0

    def _next(self):
        if self._call_idx < len(self._plan):
            item = self._plan[self._call_idx]
            self._call_idx += 1
            return item
        from miriam_agent.agents.llm import LLMResponse

        return LLMResponse(content="All done.", model=self.model)

    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        return self._next()

    async def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        r = self._next()
        if hasattr(r, "tool_calls") and r.tool_calls:
            yield {"type": "tool_call", "tool_call": r.tool_calls[0]}
        elif hasattr(r, "content") and r.content:
            for w in r.content.split():
                yield {"type": "token", "content": w + " "}
        yield {"type": "done"}

    def cost_estimate(self, usage):
        return 0.0


# -----------------------------------------------------------------------
# Tool registry tests
# -----------------------------------------------------------------------


def test_registry_builds():
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    assert len(reg) > 0
    names = reg.list_names()
    assert "get_balance" in names
    assert "send_money" in names
    assert "pay_bill" in names
    assert len(reg.auto_execute_names()) > 0
    assert len(reg.stage_confirm_names()) > 0
    assert "pay_bill" in reg.stage_confirm_names()


def test_registry_validates_required_args():
    import asyncio

    from miriam_agent.core.exceptions import ValidationError
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()

    async def _():
        try:
            await reg.execute("send_money", {"to": "bob"})
            assert False, "should have raised"
        except ValidationError as e:
            assert "amount" in str(e).lower()

    asyncio.get_event_loop().run_until_complete(_())


def test_registry_rejects_unknown_tool():
    import asyncio

    from miriam_agent.core.exceptions import ToolExecutionError
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()

    async def _():
        try:
            await reg.execute("nonexistent_tool", {})
            assert False
        except ToolExecutionError as e:
            assert "unknown" in str(e).lower() or "nonexistent" in str(e).lower()

    asyncio.get_event_loop().run_until_complete(_())


def test_llm_schemas_format():
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    schemas = reg.llm_schemas()
    assert all(s["type"] == "function" for s in schemas)
    assert all("function" in s and "name" in s["function"] for s in schemas)


# -----------------------------------------------------------------------
# Agent loop tests
# -----------------------------------------------------------------------


def test_agent_returns_final_answer_when_no_tools_called():
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    provider = MockProvider(
        [LLMResponse(content="Your balance looks solid.", model="mock")]
    )
    agent = Agent(registry=reg, provider=provider)

    async def _():
        result = await agent.run(user_id="u1", token="fake", message="how am I doing?")
        assert result.response == "Your balance looks solid."
        assert result.requires_confirmation is False
        assert len(result.tool_calls) == 0

    asyncio.get_event_loop().run_until_complete(_())


def test_agent_stages_money_action():
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    provider = MockProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "send_money",
                            "arguments": json.dumps(
                                {"to": "alice@rail.io", "amount": 150}
                            ),
                        },
                    }
                ],
            ),
        ]
    )
    agent = Agent(registry=reg, provider=provider)

    async def _():
        result = await agent.run(
            user_id="u1", token="fake", message="send $150 to alice"
        )
        assert result.requires_confirmation is True
        assert len(result.proposed_actions) == 1
        assert result.proposed_actions[0].tool_name == "send_money"
        assert "150" in result.proposed_actions[0].display_summary

    asyncio.get_event_loop().run_until_complete(_())


def test_agent_stages_bill_payment():
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    provider = MockProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {
                            "name": "pay_bill",
                            "arguments": json.dumps(
                                {
                                    "category": "airtime",
                                    "recipient": "08012345678",
                                    "amount_ngn": 1000,
                                }
                            ),
                        },
                    }
                ],
            ),
        ]
    )
    agent = Agent(registry=reg, provider=provider)

    async def _():
        result = await agent.run(
            user_id="u1", token="fake", message="buy 1000 naira airtime"
        )
        assert result.requires_confirmation is True
        assert len(result.proposed_actions) == 1
        assert result.proposed_actions[0].tool_name == "pay_bill"
        summary = result.proposed_actions[0].display_summary
        assert "airtime" in summary and "1000" in summary

    asyncio.get_event_loop().run_until_complete(_())


def test_agent_executes_readonly_tool_then_returns_answer():
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.llm import LLMResponse
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    provider = MockProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "get_balance", "arguments": "{}"},
                    }
                ],
            ),
            LLMResponse(
                content="Your total balance across wallets is $2,500.", model="mock"
            ),
        ]
    )
    agent = Agent(registry=reg, provider=provider)

    async def _():
        result = await agent.run(
            user_id="u1", token="fake", message="what's my balance?"
        )
        assert "$2,500" in result.response
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "get_balance"
        assert result.requires_confirmation is False

    asyncio.get_event_loop().run_until_complete(_())


# -----------------------------------------------------------------------
# System prompt tests
# -----------------------------------------------------------------------


def test_system_prompt_has_voice():
    from miriam_agent.agents.system_prompt import build_system_prompt

    p = build_system_prompt(
        user_context={"name": "Tobi"},
        memory_facts=[{"type": "goal", "content": "build emergency fund"}],
    )
    assert "Not an app, not a dashboard" in p
    assert "Tobi" in p
    assert "emergency fund" in p


def test_system_prompt_uses_real_context_numbers():
    from miriam_agent.agents.system_prompt import build_system_prompt

    p = build_system_prompt(
        user_context={"monthly_income": 8000, "risk_tolerance": "aggressive"}
    )
    assert "8000" in p or "8,000" in p
    assert "aggressive" in p


# -----------------------------------------------------------------------
# Security / auth tests
# -----------------------------------------------------------------------


def test_jwt_roundtrip():
    from miriam_agent.auth.jwt import create_token, decode_token, get_user_id

    token = create_token("user_123", {"email": "a@test.com", "roles": ["user"]})
    payload = decode_token(token)
    assert payload["sub"] == "user_123"
    assert payload["email"] == "a@test.com"
    assert get_user_id(token) == "user_123"


def test_jwt_expiration_rejects():
    from miriam_agent.auth.jwt import create_token, decode_token
    from miriam_agent.core.exceptions import AuthenticationError

    token = create_token("u1", expires_minutes=-1)  # already expired
    try:
        decode_token(token)
        assert False, "should have raised"
    except AuthenticationError:
        pass


def test_rbac_blocks_mutation():
    from miriam_agent.auth.rbac import can_execute, require_tool_access
    from miriam_agent.core.exceptions import AuthorizationError

    assert can_execute({"user"}, "get_balance") is True
    assert can_execute({"user"}, "send_money") is False
    assert can_execute({"verified"}, "send_money") is True
    try:
        require_tool_access({"user"}, "send_money")
        assert False, "should have raised"
    except AuthorizationError:
        pass


def test_security_encrypt_decrypt():
    from miriam_agent.core.security import (
        decrypt_value,
        encrypt_value,
        generate_idempotency_key,
    )

    enc = encrypt_value("secret-ssn-123")
    assert decrypt_value(enc) == "secret-ssn-123"
    key = generate_idempotency_key()
    assert key.startswith("miriam_") and len(key) > 20


# -----------------------------------------------------------------------
# Concentrate provider tests
# -----------------------------------------------------------------------


def test_concentrate_input_serialization_with_tool_pairing():
    from miriam_agent.agents.concentrate import to_input_items
    from miriam_agent.agents.llm import ChatMessage

    items = to_input_items(
        [
            ChatMessage(role="system", content="sys"),
            ChatMessage(role="user", content="hello"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_balance", "arguments": "{}"},
                    }
                ],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="call_1",
                name="get_balance",
                content='{"total": 2500}',
            ),
        ]
    )
    assert items[0] == {"role": "system", "content": "sys"}
    assert items[1] == {"role": "user", "content": "hello"}
    assert items[2] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_balance",
        "arguments": "{}",
    }
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"total": 2500}',
    }


def test_concentrate_tool_flattening():
    from miriam_agent.agents.concentrate import convert_tools

    out = convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "get_balance",
                    "description": "Check balance",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )
    assert out == [
        {
            "type": "function",
            "name": "get_balance",
            "description": "Check balance",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]
    assert convert_tools(None) == []


def test_concentrate_parse_response_output():
    from miriam_agent.agents.concentrate import parse_response_output

    content, calls = parse_response_output(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Here it is: "}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_9",
                    "name": "get_balance",
                    "arguments": "{}",
                },
            ],
        }
    )
    assert content == "Here it is: "
    assert calls[0]["id"] == "call_9"
    assert calls[0]["function"]["name"] == "get_balance"


def test_concentrate_parse_usage_and_stream_event_shapes():
    from miriam_agent.agents.concentrate import parse_stream_event, parse_usage

    usage = parse_usage({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (
        10,
        5,
        15,
    )
    # Doc shape 1: data.type + data.delta
    ev = parse_stream_event({"type": "response.output_text.delta", "delta": "Hello"})
    assert ev["type"] == "response.output_text.delta"
    assert ev["delta"] == "Hello"
    # Doc shape 2: data.event + data.data.arguments
    ev = parse_stream_event(
        {"event": "response.function_call_arguments.done", "data": {"arguments": "{}"}}
    )
    assert ev["type"] == "response.function_call_arguments.done"
    assert ev["arguments"] == "{}"


def test_concentrate_complete_roundtrip():
    import httpx

    from miriam_agent.agents.concentrate import ConcentrateProvider
    from miriam_agent.agents.llm import ChatMessage

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-terra"
        assert body["input"][0]["role"] == "user"
        assert body["input"][0]["content"] == "hi"
        assert "routing" in body
        assert body["tools"][0]["strict"] is False
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "model": "openai/gpt-5.6-terra",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Hello!"}],
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = ConcentrateProvider(api_key="sk-cn-test", http_client=client)
    result = asyncio.get_event_loop().run_until_complete(
        provider.complete(
            [ChatMessage(role="user", content="hi")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_balance",
                        "description": "balance",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    )
    assert result.content == "Hello!"
    assert result.usage["prompt_tokens"] == 7
    assert result.usage["cached_tokens"] == 0
    asyncio.get_event_loop().run_until_complete(provider.aclose())


def test_concentrate_stream_roundtrip():
    import httpx

    from miriam_agent.agents.concentrate import ConcentrateProvider
    from miriam_agent.agents.llm import ChatMessage

    sse = "\n".join(
        [
            "event: response.output_text.delta",
            'data: {"type": "response.output_text.delta", "delta": "Sure:"}',
            "",
            "event: response.function_call_arguments.delta",
            'data: {"type": "response.function_call_arguments.delta",'
            ' "call_id": "call_1", "delta": "{}"}',
            "",
            "event: response.function_call_arguments.done",
            'data: {"type": "response.function_call_arguments.done",'
            ' "call_id": "call_1", "name": "get_balance", "arguments": "{}"}',
            "",
            "event: response.completed",
            'data: {"type": "response.completed", "response": {"usage":'
            ' {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}}}',
            "",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode())

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    provider = ConcentrateProvider(api_key="sk-cn-test", http_client=client)

    async def _():
        events = []
        async for ev in provider.stream([ChatMessage(role="user", content="hi")]):
            events.append(ev)
        return events

    events = asyncio.get_event_loop().run_until_complete(_())
    assert events[0] == {"type": "token", "content": "Sure:"}
    tc = [e for e in events if e["type"] == "tool_call"][0]
    assert tc["tool_call"]["function"]["name"] == "get_balance"
    assert events[-1]["type"] == "done"
    asyncio.get_event_loop().run_until_complete(provider.aclose())


def test_concentrate_agent_loop_multi_round_pairing():
    """The agent loop must re-emit assistant tool_calls before the results."""
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.agents.llm import ChatMessage, LLMResponse
    from miriam_agent.tools import build_tool_registry

    sent_message_lists: list[list[ChatMessage]] = []

    class RecordingProvider(MockProvider):
        async def complete(
            self, messages, tools=None, temperature=None, max_tokens=None
        ):
            sent_message_lists.append(list(messages))
            return self._next()

    reg = build_tool_registry()
    provider = RecordingProvider(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "get_balance", "arguments": "{}"},
                    }
                ],
            ),
            LLMResponse(content="Your balance is $2,500.", model="mock"),
        ]
    )
    agent = Agent(registry=reg, provider=provider)

    asyncio.get_event_loop().run_until_complete(
        agent.run(user_id="u1", token="fake", message="what's my balance?")
    )
    assert len(sent_message_lists) == 2
    round2 = sent_message_lists[1]
    # assistant tool_calls message must precede the tool result
    assert round2[-2].role == "assistant"
    assert round2[-2].tool_calls
    assert round2[-1].role == "tool"
    assert round2[-1].tool_call_id == "tc1"


# -----------------------------------------------------------------------
# FastAPI / API smoke tests
# -----------------------------------------------------------------------


def test_api_app_boots():
    from miriam_agent.api.main import app

    assert app.title == "Miriam Financial Agent API"


def test_health_endpoint():
    from fastapi.testclient import TestClient

    from miriam_agent.api.main import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-x"])
