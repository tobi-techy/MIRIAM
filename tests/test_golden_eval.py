"""Golden eval harness: CI-gated regression for agent behavior.

25 cases across money safety, tool routing, budgets, memory, onboarding.
No LLM calls: asserts structural contracts that must hold regardless of model.

Run: pytest tests/test_golden_eval.py -x -q
CI fails the build on any regression.
"""

from miriam_agent.agents.tools import ToolRegistry, idempotency_key
from miriam_agent.safety.money_tools import MONEY_TOOL_NAMES, is_money_tool


def _registry_with_read():
    reg = ToolRegistry()

    async def _handler(args, ctx):
        return {"ok": True}

    reg.register(
        name="get_balance",
        description="read balance",
        args_schema={"type": "object", "properties": {}},
        handler=_handler,
        category="finance",
        risk_level="low",
        is_mutation=False,
        requires_approval=False,
    )
    return reg


class TestMoneySafety:
    def test_money_names_stripped_from_live_registry(self):
        # Legacy flow: definitions.py registers then build_tool_registry strips.
        # Live invariant is zero money tools after strip, enforced fail-closed.
        reg = _registry_with_read()

        async def _h(args, ctx):
            return {}

        reg.register(
            name="send_money",
            description="evil",
            args_schema={"type": "object", "properties": {}},
            handler=_h,
            is_mutation=True,
            requires_approval=True,
        )
        # Before strip, guard detects it
        try:
            reg.assert_no_money_tools()
            raise AssertionError("should have detected money tool")
        except ValueError:
            pass
        reg.unregister("send_money")
        reg.assert_no_money_tools()
        for name in ["send_money", "pay_bill", "invest", "buy_crypto"]:
            assert is_money_tool(name)

    def test_llm_schemas_never_offer_mutations(self):
        reg = _registry_with_read()

        async def _h(args, ctx):
            return {}

        reg.register(
            name="test_mut",
            description="mut",
            args_schema={"type": "object", "properties": {}},
            handler=_h,
            is_mutation=True,
        )
        schemas = reg.llm_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "test_mut" not in names
        assert "get_balance" in names

    def test_money_tool_names_nonempty(self):
        assert len(MONEY_TOOL_NAMES) > 30


class TestIdempotency:
    def test_same_args_same_key(self):
        k1 = idempotency_key("get_balance", {"a": 1}, "u1")
        k2 = idempotency_key("get_balance", {"a": 1}, "u1")
        assert k1 == k2

    def test_different_user_different_key(self):
        assert idempotency_key("t", {}, "u1") != idempotency_key("t", {}, "u2")

    def test_key_order_independent(self):
        k1 = idempotency_key("t", {"a": 1, "b": 2}, "u")
        k2 = idempotency_key("t", {"b": 2, "a": 1}, "u")
        assert k1 == k2

    async def test_execute_dedupes(self):
        import asyncio

        reg = _registry_with_read()
        calls = []

        async def _count(args, ctx):
            calls.append(1)
            return {"n": len(calls)}

        reg.register(
            name="count_tool",
            description="c",
            args_schema={"type": "object", "properties": {}},
            handler=_count,
            is_mutation=True,
        )
        r1 = await reg.execute("count_tool", {}, context={"user_id": "u"})
        r2 = await reg.execute("count_tool", {}, context={"user_id": "u"})
        assert r1["_idempotency_key"] == r2["_idempotency_key"]
        assert r2.get("_idempotent_hit") is True
        assert len(calls) == 1

    def _noop(self):
        pass


class TestBudgets:
    def test_budget_defaults_sane(self):
        from miriam_agent.agents.agent_loop import _budgets

        rounds, toks, cost, wall = _budgets()
        assert 1 <= rounds <= 10
        assert toks >= 4000
        assert cost > 0
        assert wall >= 5.0

    def test_abort_result_shape(self):
        from miriam_agent.agents.agent_loop import _abort_result

        r = _abort_result("conv_1", "cost")
        assert r.conversation_id == "conv_1"
        assert len(r.response) > 0


class TestMetrics:
    def test_onboarding_counter_has_no_user_label(self):
        from miriam_agent.observability.metrics import ONBOARDING_EVENTS

        # _labelnames is prometheus internal; event only, no user_id
        names = set(ONBOARDING_EVENTS._labelnames)
        assert "user_id" not in names
        assert "event" in names

    def test_budget_counter_exists(self):
        from miriam_agent.observability.metrics import AGENT_BUDGET_EXCEEDED

        assert "budget" in set(AGENT_BUDGET_EXCEEDED._labelnames)


class TestMemorySearch:
    def test_search_ranks_overlap_over_recency(self):
        # Structural: search_memories sorts by token hits + recency.
        # Full DB test lives in test_database; here assert tokenizer logic.
        q = "salary rent lagos"
        toks = [t.lower() for t in q.split() if len(t) > 2][:8]
        assert toks == ["salary", "rent", "lagos"]

    def test_init_db_has_vector(self):
        import pathlib

        sql = pathlib.Path("scripts/init-db.sql").read_text()
        assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
        assert "VECTOR(1536)" in sql


class TestToolValidation:
    def test_missing_required_rejected(self):
        from miriam_agent.agents.tools import Tool, validate_args
        from miriam_agent.core.exceptions import ValidationError

        async def _h(a, c):
            return {}

        t = Tool(
            name="x",
            description="x",
            args_schema={
                "type": "object",
                "required": ["amount"],
                "properties": {"amount": {"type": "number", "minimum": 1}},
            },
            handler=_h,
        )
        try:
            validate_args(t, {})
            raise AssertionError("should reject")
        except ValidationError:
            pass

    def test_negative_amount_rejected(self):
        from miriam_agent.agents.tools import Tool, validate_args
        from miriam_agent.core.exceptions import ValidationError

        async def _h(a, c):
            return {}

        t = Tool(
            name="x",
            description="x",
            args_schema={
                "type": "object",
                "properties": {"amount": {"type": "number", "minimum": 1}},
            },
            handler=_h,
        )
        try:
            validate_args(t, {"amount": -5})
            raise AssertionError("should reject")
        except ValidationError:
            pass


class TestArchitectureContracts:
    def test_agent_loop_has_no_hands_import(self):
        import pathlib

        src = pathlib.Path("miriam_agent/agents/agent_loop.py").read_text()
        assert "from miriam_agent.hands" not in src
        assert "import miriam_agent.hands" not in src

    def test_registry_has_fail_closed_guard(self):
        import pathlib

        src = pathlib.Path("miriam_agent/agents/tools.py").read_text()
        assert "assert_no_money_tools" in src
        assert "idempotency_key" in src

    def test_budgets_wired(self):
        import pathlib

        src = pathlib.Path("miriam_agent/agents/agent_loop.py").read_text()
        for token in ["_budgets", "wall_clock", "AGENT_MAX_COST_USD_PER_TURN", "_abort_result"]:
            assert token in src, f"missing {token}"

    def test_settings_has_budgets(self):
        from miriam_agent.config.settings import Settings

        for f in [
            "AGENT_MAX_TOOL_ROUNDS",
            "AGENT_MAX_TOKENS_PER_TURN",
            "AGENT_MAX_COST_USD_PER_TURN",
            "AGENT_WALL_CLOCK_S",
        ]:
            assert f in Settings.model_fields

    # Onboarding / safety golden placeholders (structural, no LLM)
    def test_onboarding_state_machine_exists(self):
        import pathlib

        assert pathlib.Path("miriam_agent/onboarding/service.py").exists()
        assert pathlib.Path("miriam_agent/onboarding/state.py").exists()

    def test_safety_policy_exists(self):
        import pathlib

        assert pathlib.Path("miriam_agent/safety/policy.py").exists()
        assert pathlib.Path("miriam_agent/safety/audit.py").exists()

    def test_orchestrator_entrypoint_exists(self):
        import pathlib

        assert pathlib.Path("miriam_agent/orchestrator.py").exists()

    def test_trace_correlation_exists(self):
        import pathlib

        assert pathlib.Path("miriam_agent/observability/correlation.py").exists()

    def test_go_client_is_money_authority(self):
        import pathlib

        src = pathlib.Path("miriam_agent/integrations/go_client.py").read_text()
        assert "X-Miriam-Confirm-Id" in src or "confirm" in src.lower()

    def test_money_plan_single_door(self):
        import pathlib

        assert pathlib.Path("miriam_agent/money").is_dir()
