"""Retirement vault wiring tests.

Covers the plan's Workstream 6: vault context shapes, preview pass-through,
one-planner suppression, registry safety, copy guard, staged activate, and
the unseeded-strategies spoken line.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _go_client_for(handler):
    from miriam_agent.integrations.go_client import GoBackendClient

    transport = httpx.MockTransport(handler)
    client = GoBackendClient("http://go.test")
    _run(client._client.aclose())
    client._client = httpx.AsyncClient(
        base_url="http://go.test",
        transport=transport,
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "RailApp",
        },
    )
    return client


ACTIVE_VAULT = {
    "exists": True,
    "plan": "lock-10",
    "principal": 900,
    "earnings": 100,
    "total": 1000,
    "unlock_at": "2045-01-01",
    "penalty_if_withdraw_today": 10,
    "auto_pct": 20,
    "tier": "Balanced",
    "status": "active",
    "lots": [{"id": "lot-1", "principal": 900}],
}

PREVIEW_FIXTURE = {
    "principal_out": 900,
    "earnings_out": 90,
    "penalty": 9,
    "payout": 981,
    "qualified": False,
    "unlock_at": "2045-01-01",
}

SEEDED = {"status": "seeded", "strategies": [{"id": "steady"}]}
UNSEEDED = {"status": "unseeded", "strategies": []}


class _FakeGo:
    def __init__(self, vault=None, strategies=None, preview=None):
        self.vault = vault
        self.strategies = strategies if strategies is not None else SEEDED
        self.preview = preview if preview is not None else PREVIEW_FIXTURE
        self.posts: list[str] = []

    async def get_vault(self, token):
        assert token == "tok"
        if self.vault is None:
            return {"exists": False}
        return dict(self.vault)

    async def list_vault_strategies(self, token):
        return dict(self.strategies)

    async def get_vault_activity(self, token, limit=None):
        return [{"id": "lot-1"}]

    async def preview_vault_withdraw(self, token, amount):
        assert amount == 100
        return dict(self.preview)


def _patch_go(monkeypatch, fake):
    import miriam_agent.tools.vault_definitions as vd

    monkeypatch.setattr(vd, "get_go_client", lambda: fake, raising=False)
    import miriam_agent.integrations.go_client as gc

    monkeypatch.setattr(gc, "get_go_client", lambda: fake, raising=False)


def test_get_vault_returns_exists_false_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/vault"
        return httpx.Response(404, json={"error": "not_found"})

    client = _go_client_for(handler)
    out = _run(client.get_vault("tok"))
    assert out == {"exists": False}
    _run(client.close())


def test_get_vault_reads_and_preview_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/vault":
            return httpx.Response(200, json=ACTIVE_VAULT)
        if path == "/api/v1/vault/strategies":
            return httpx.Response(200, json=SEEDED)
        if path == "/api/v1/vault/activity":
            return httpx.Response(200, json={"activity": [{"id": "lot-1"}]})
        if path == "/api/v1/vault/preview-withdraw":
            seen["amount"] = httpx.URL(str(request.url)).params.get("amount")
            return httpx.Response(200, json=PREVIEW_FIXTURE)
        raise AssertionError(path)

    client = _go_client_for(handler)
    vault = _run(client.get_vault("tok"))
    assert vault["exists"] is True
    assert vault["total"] == 1000
    preview = _run(client.preview_vault_withdraw("tok", 100))
    assert preview == PREVIEW_FIXTURE
    assert seen["amount"] == "100"
    activity = _run(client.get_vault_activity("tok"))
    assert activity == [{"id": "lot-1"}]
    _run(client.close())


def test_client_has_no_vault_write_or_withdraw_submit():
    from miriam_agent.integrations.go_client import GoBackendClient

    assert hasattr(GoBackendClient, "preview_vault_withdraw")
    for banned in ("create_vault", "update_vault", "submit_vault_withdraw"):
        assert not hasattr(GoBackendClient, banned), banned


def test_vault_context_no_vault(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    _patch_go(monkeypatch, _FakeGo(vault=None))
    reg = build_tool_registry()
    out = _run(reg.execute("get_vault_context", {}, {"token": "tok"}))
    assert out["exists"] is False


def test_vault_context_active_vault(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    _patch_go(monkeypatch, _FakeGo(vault=ACTIVE_VAULT))
    reg = build_tool_registry()
    out = _run(reg.execute("get_vault_context", {}, {"token": "tok"}))
    assert out["exists"] is True
    assert out["total"] == 1000
    assert out["tier_label"] == "Balanced"
    assert out["unlock_at"] == "2045-01-01"
    assert out["plan_available"] is True


def test_vault_context_plan_unavailable(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    _patch_go(monkeypatch, _FakeGo(vault=ACTIVE_VAULT, strategies=UNSEEDED))
    reg = build_tool_registry()
    out = _run(reg.execute("get_vault_context", {}, {"token": "tok"}))
    assert out["plan_available"] is False
    assert "not live" in out["spoken"]


def test_preview_withdraw_passes_go_through_unchanged(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    _patch_go(monkeypatch, _FakeGo(vault=ACTIVE_VAULT))
    reg = build_tool_registry()
    out = _run(reg.execute("preview_withdraw", {"amount": 100}, {"token": "tok"}))
    assert out["principal_out"] == 900
    assert out["earnings_out"] == 90
    assert out["penalty"] == 9
    assert out["payout"] == 981
    assert out["qualified"] is False


def test_money_plan_with_active_vault_emits_no_draft_book():
    from miriam_agent.money.plan import build_money_plan

    fixture_profile = {
        "country": "US",
        "currency": "USD",
        "age": 34,
        "dependents": 0,
        "income_amount": "9000",
        "income_frequency": "monthly",
        "income_volatility": "steady",
        "fixed_costs": "4200",
        "variable_spend": "1600",
        "debts": [],
        "cash_on_hand": "38000",
        "goals": [{"label": "retirement", "horizon_months": 120}],
        "job_stability": "stable",
        "risk_tolerance": "medium",
        "can_self_custody": True,
    }
    baseline = build_money_plan(dict(fixture_profile))
    assert baseline.glider.kind == "draft", "fixture must draft without a vault"
    vault_state = {
        "active": True,
        "tier_label": "Balanced",
        "vault_pct": 20,
        "unlock_date": "2045",
        "total": 1000,
    }
    plan = build_money_plan(dict(fixture_profile), None, None, vault_state)
    assert plan.glider.kind == "none"
    assert plan.glider.draft is None
    rules = " ".join(plan.automation_rules).lower()
    assert "locked dollar sleeve" in rules
    assert "balanced" in rules


def test_no_vault_mutation_in_llm_schemas():
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    reg = build_tool_registry()
    names = {s["function"]["name"] for s in reg.llm_schemas()}
    assert {"get_vault_context", "preview_plan", "preview_withdraw", "propose_vault_plan"} <= names
    for banned in ("create_vault", "update_vault", "submit_vault_withdraw"):
        assert banned not in names, banned
    preview = reg.get("preview_withdraw")
    assert preview.is_mutation is False
    assert preview.requires_approval is False


def test_copy_guard_rejects_banned_words():
    from miriam_agent.money.vault_copy import BANNED_VAULT_WORDS, check_vault_copy

    assert BANNED_VAULT_WORDS
    for word in BANNED_VAULT_WORDS:
        with pytest.raises(ValueError):
            check_vault_copy(f"your locked sleeve uses {word} rails")
    check_vault_copy("Locked dollar retirement sleeve. Deposits can come out.")


def test_propose_stages_confirm_and_never_posts(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    fake = _FakeGo(vault=None)

    def fail_post(*a, **k):
        raise AssertionError("vault tools must never POST")

    fake.post = fail_post  # type: ignore[attr-defined]
    _patch_go(monkeypatch, fake)
    reg = build_tool_registry()
    out = _run(
        reg.execute(
            "propose_vault_plan",
            {"tier": "Balanced", "retirement_age": 60, "vault_pct": 20},
            {"token": "tok"},
        )
    )
    assert out["staged"] is True
    assert out["draft"]["tier"] == "Balanced"
    assert len(out["spoken_four_facts"]) == 4
    assert out["vault_envelope"]["method"] == "POST"
    assert out["vault_envelope"]["payload"]["tier"] == "Balanced"


def test_unseeded_propose_speaks_not_live_no_ids(monkeypatch):
    from miriam_agent.tools import vault_definitions as vd  # noqa: F401
    from miriam_agent.tools import build_tool_registry

    _patch_go(monkeypatch, _FakeGo(vault=None, strategies=UNSEEDED))
    reg = build_tool_registry()
    out = _run(
        reg.execute(
            "propose_vault_plan",
            {"tier": "Steady", "retirement_age": 60, "vault_pct": 10},
            {"token": "tok"},
        )
    )
    assert out["staged"] is False
    assert "not live" in out["spoken"]
    assert "draft" not in out
