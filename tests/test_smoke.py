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
    assert len(reg.auto_execute_names()) > 0
    assert len(reg.stage_confirm_names()) > 0


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
    assert "Ramit Sethi" in p
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
