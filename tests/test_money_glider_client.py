"""Glider client behaviour, against a fake transport.

No live API, ever. The architecture contract requires every external provider to
be constructible with a fake transport, so this is the seam.

Two properties get adversarial tests rather than happy-path ones, because they
are the ones that lose money: an asset id must never be guessed, and enrollment
must never be committed by the agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-tests")

from decimal import Decimal

import httpx
import pytest

from miriam_agent.core.exceptions import ConfigurationError, IntegrationError
from miriam_agent.integrations.glider_client import (
    GliderClient,
    is_caip19,
    require_asset_id,
)
from miriam_agent.money.glider import build_draft
from miriam_agent.money.schema import AllocationBook
from miriam_agent.money.templates import (
    MIRIAM_BUILD,
    MIRIAM_CORE,
    MIRIAM_PRESERVE,
    allocation_payload,
    select_template,
    validate_weights,
)

CAIP_USDC = "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
CAIP_INDEX = "eip155:8453/erc20:0x1111111111111111111111111111111111111111"
CAIP_RWA = "eip155:8453/erc20:0x2222222222222222222222222222222222222222"
CAIP_PAPER = "eip155:8453/erc20:0x4444444444444444444444444444444444444444"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def ok(data, **extra):
    return httpx.Response(200, json={"success": True, "data": data, **extra})


def err(status, code, message, details=None):
    body = {"success": False, "error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return httpx.Response(status, json=body)


def client_for(handler, **kwargs):
    return GliderClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Polling must not actually wait in tests."""

    async def _instant(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


# ---------------------------------------------------------------------------
# Asset ids are never guessed
# ---------------------------------------------------------------------------


def test_require_asset_id_rejects_a_symbol():
    """There is no fallback: a plausible-but-wrong id is a real token."""
    with pytest.raises(IntegrationError, match="CAIP-19"):
        require_asset_id("USDC")


def test_require_asset_id_rejects_a_bare_hex_address():
    with pytest.raises(IntegrationError, match="CAIP-19"):
        require_asset_id("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")


def test_require_asset_id_accepts_a_real_caip19_id():
    assert require_asset_id(CAIP_USDC) == CAIP_USDC


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (CAIP_USDC, True),
        (
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKz/spl:So11111111111111111111111111111111111111112",
            True,
        ),
        ("USDC", False),
        ("", False),
    ],
)
def test_is_caip19_shape_check(value, expected):
    assert is_caip19(value) is expected


def test_discover_assets_reads_ids_from_live_positions():
    """Observing a real id is the only safe way to learn one."""

    def handler(request):
        return ok(
            {
                "portfolioId": "p1",
                "assets": [
                    {"assetId": CAIP_USDC, "symbol": "USDC", "valueUsd": "10.0"},
                    {"assetId": "not-an-id", "symbol": "JUNK", "valueUsd": "1.0"},
                ],
            }
        )

    client = client_for(handler)
    assert _run(client.discover_assets("p1")) == {"USDC": CAIP_USDC}


# ---------------------------------------------------------------------------
# Enrollment is prepared, never committed
# ---------------------------------------------------------------------------


def test_enrollment_intent_is_user_signed_and_commits_nothing():
    prepared = {
        "flowId": "flow_abc123",
        "accountIndex": "7",
        "agentAccountId": "eip155:0:0x1111111111111111111111111111111111111111",
        "message": {"kind": "ecdsa", "raw": "0xdeadbeef"},
    }
    intent = GliderClient.enrollment_intent(prepared, strategy_name="Miriam Core 70/30")
    assert intent["requires_user_signature"] is True
    assert intent["action"] == "user_signed_enrollment"
    assert intent["flow_id"] == "flow_abc123"


def test_signature_kind_is_surfaced_not_assumed():
    """SVM and smart-contract wallets sign differently; the client must not guess."""
    for kind in ("ecdsa", "typed-data", "solana-message"):
        intent = GliderClient.enrollment_intent({"message": {"kind": kind}})
        assert intent["signature_kind"] == kind


def test_the_client_cannot_commit_an_enrollment():
    """Stage 2 is deliberately absent, so no caller can auto-enroll.

    Checked against the source rather than the attributes: a committing call
    added later under a different name must not slip past this guard.
    """
    import inspect

    from miriam_agent.integrations import glider_client as module

    source = inspect.getsource(module)
    assert '"/enroll/signature"' in source
    assert '"/enroll"' not in source, "stage 2 must not be implemented"


def test_the_client_exposes_no_withdrawal_route_or_method():
    """Withdrawals are user-signed and app-only; the agent has no path to one."""
    import inspect

    from miriam_agent.integrations import glider_client as module

    source = inspect.getsource(module)
    assert '"/withdraw' not in source
    for name, _member in inspect.getmembers(GliderClient):
        assert "withdraw" not in name.casefold(), f"unexpected method {name}"


def test_prepare_enrollment_posts_stage_one():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok({"flowId": "flow_1", "message": {"kind": "ecdsa"}})

    client = client_for(handler)
    result = _run(
        client.prepare_enrollment("strat_1", "eip155:0:0xabc", [8453], "ECDSA")
    )
    assert result["flowId"] == "flow_1"
    assert seen[0].url.path.endswith("/enroll/signature")
    body = json.loads(seen[0].content)
    assert body["chainIds"] == [8453]
    assert body["accountType"] == "ECDSA"


# ---------------------------------------------------------------------------
# Envelope handling and errors
# ---------------------------------------------------------------------------


def test_strategy_list_unwraps_the_envelope_and_keeps_the_cursor():
    def handler(request):
        return ok({"strategies": [{"strategyId": "s1"}]}, nextCursor="eyJjIjoi")

    client = client_for(handler)
    strategies, cursor = _run(client.list_strategies())
    assert strategies == [{"strategyId": "s1"}]
    assert cursor == "eyJjIjoi"


def test_error_envelope_becomes_an_actionable_integration_error():
    def handler(request):
        return err(
            400,
            "API_400",
            "Request validation failed",
            ["allocation.assets: Allocation weights must sum to 100"],
        )

    client = client_for(handler)
    with pytest.raises(IntegrationError) as caught:
        _run(client.get_strategy("s1"))
    message = str(caught.value)
    assert "API_400" in message
    assert "sum to 100" in message


def test_missing_api_key_is_a_configuration_error_not_a_silent_failure():
    client = GliderClient(api_key="", transport=httpx.MockTransport(lambda r: ok({})))
    with pytest.raises(ConfigurationError):
        _run(client.whoami())


def test_scopes_route_is_reachable_without_a_key():
    """The one public route must not be blocked by the key guard."""

    def handler(request):
        return ok({"scopes": ["strategies:read"]})

    client = GliderClient(api_key="", transport=httpx.MockTransport(handler))
    assert _run(client.list_scopes()) == {"scopes": ["strategies:read"]}


def test_retry_after_is_honoured_on_a_429(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _record)

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return ok({"portfolioId": "p1"})

    client = client_for(handler, max_retries=2)
    assert _run(client.get_portfolio("p1")) == {"portfolioId": "p1"}
    assert calls["n"] == 2
    assert slept == [2.0]


def test_writes_are_not_retried():
    """A repeated unanchored write is how a double-rebalance happens."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"success": False, "error": {"code": "x"}})

    client = client_for(handler, max_retries=3)
    with pytest.raises(IntegrationError):
        _run(client.rebalance("p1"))
    assert calls["n"] == 1


def test_malformed_identifier_is_refused_before_the_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return ok({})

    client = client_for(handler)
    with pytest.raises(IntegrationError, match="malformed"):
        _run(client.get_portfolio("../secrets"))
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Async operations are polled, never assumed
# ---------------------------------------------------------------------------


def test_rebalance_returns_the_operation_to_poll():
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            202, json={"success": True, "data": {"operationId": "op_1"}}
        )

    client = client_for(handler)
    assert _run(client.rebalance("p1"))["operationId"] == "op_1"


def test_await_operation_polls_until_terminal():
    states = iter(["accepted", "running", "completed"])

    def handler(request):
        return ok({"state": next(states), "operationId": "op_1"})

    client = client_for(handler)
    result = _run(client.await_operation("p1", "op_1"))
    assert result["state"] == "completed"
    assert result["settled"] is True


def test_await_operation_reports_unsettled_rather_than_guessing():
    """ "Still running" is an honest answer; a wrong one is not."""

    def handler(request):
        return ok({"state": "running"})

    client = client_for(handler)
    client.max_polls = 2
    result = _run(client.await_operation("p1", "op_1"))
    assert result["settled"] is False
    assert "may still complete" in result["note"]


def test_positions_passes_glider_warnings_through():
    """Partial failures come back as 200 + warnings, not an error."""

    def handler(request):
        return ok(
            {
                "portfolioId": "p1",
                "totalValueUsd": "0",
                "assets": [],
                "warnings": [{"kind": "RPC_ERROR", "message": "chain 1 RPC timeout"}],
            }
        )

    client = client_for(handler)
    data = _run(client.get_positions("p1"))
    assert data["warnings"][0]["kind"] == "RPC_ERROR"


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_invalid_weights_are_refused_locally_without_a_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return ok({"valid": True})

    client = client_for(handler)
    with pytest.raises(IntegrationError, match="sum to 100"):
        _run(
            client.validate_strategy(
                {
                    "name": "bad",
                    "allocation": {"assets": [{"assetId": CAIP_USDC, "weight": "90"}]},
                }
            )
        )
    assert called["n"] == 0


def test_a_valid_payload_reaches_the_validate_endpoint():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok({"valid": True})

    client = client_for(handler)
    payload = {
        "name": "Miriam Core 70/30",
        "allocation": {
            "assets": [
                {"assetId": CAIP_INDEX, "weight": "70"},
                {"assetId": CAIP_USDC, "weight": "30"},
            ]
        },
    }
    assert _run(client.validate_strategy(payload)) == {"valid": True}
    assert seen[0].url.path.endswith("/strategies/validate")


def test_validate_weights_catches_duplicates_decimals_and_shape():
    problems = validate_weights(
        [
            {"assetId": CAIP_USDC, "weight": "33.333"},
            {"assetId": CAIP_USDC, "weight": "10"},
            {"assetId": "nonsense", "weight": "10"},
        ]
    )
    joined = " ".join(problems)
    assert "duplicate" in joined
    assert "2 decimals" in joined
    assert "CAIP-19" in joined
    assert "sum to 100" in joined


def test_allocation_payload_sums_to_one_hundred():
    assets = {
        "broad_equity_index": CAIP_INDEX,
        "tokenized_real_world_assets": CAIP_RWA,
        "high_quality_stablecoin": CAIP_USDC,
        "tokenized_short_duration_paper": CAIP_PAPER,
    }
    payload = allocation_payload(MIRIAM_CORE, assets)
    rows = payload["allocation"]["assets"]
    total = sum(Decimal(row["weight"]) for row in rows)
    assert total == Decimal("100")
    assert validate_weights(rows) == []
    assert payload["schedule"] == {"type": "interval", "frequency": "monthly"}


def test_allocation_payload_refuses_unresolved_assets():
    """Dropping a sleeve silently would leave weights that do not sum to 100."""
    with pytest.raises(ValueError, match="resolved assets"):
        allocation_payload(MIRIAM_CORE, {"broad_equity_index": CAIP_INDEX})


# ---------------------------------------------------------------------------
# Template selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("growth", "expected"),
    [
        ("80", "miriam_build_80_20"),
        ("70", "miriam_core_70_30"),
        ("60", "miriam_core_70_30"),
        ("40", "miriam_preserve_40_60"),
    ],
)
def test_template_selection_maps_the_book(growth, expected):
    book = AllocationBook(
        growth_pct=Decimal(growth),
        defensive_pct=Decimal("100") - Decimal(growth),
        investable_surplus=Decimal("100"),
    )
    assert select_template(book).template_id == expected


def test_no_template_when_there_is_no_book():
    """``None`` is a real answer: emergency money has no strategy."""
    gated = AllocationBook(gated=True, growth_pct=Decimal("0"))
    assert select_template(gated) is None

    short = AllocationBook(short_horizon=True, growth_pct=Decimal("0"))
    assert select_template(short) is None


def test_templates_never_hardcode_an_asset_address():
    """A baked-in CAIP-19 is a dead contract waiting to happen."""
    import inspect

    from miriam_agent.money import templates as module

    source = inspect.getsource(module)
    assert "0x" not in source, "templates must describe asset classes, not addresses"


# ---------------------------------------------------------------------------
# Drafts: validated when possible, local when not, never submitted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [MIRIAM_CORE, MIRIAM_PRESERVE, MIRIAM_BUILD],
)
def test_draft_weights_sum_to_one_hundred_for_every_template(template):
    draft = build_draft(template)
    assert sum((w.weight for w in draft.weights), Decimal("0")) == Decimal("100")
    assert draft.book == f"{int(template.growth_pct)}/{int(template.defensive_pct)}"
    assert draft.schedule == {"type": "interval", "frequency": "monthly"}
    assert draft.submitted is False


def test_draft_carries_no_asset_ids_until_a_real_source_supplies_them():
    draft = build_draft(MIRIAM_CORE)
    assert all(w.asset_id == "" for w in draft.weights)
    assert "need resolving" in draft.note


def test_draft_accepts_resolved_ids_without_inventing_any():
    resolved = {"broad_equity_index": CAIP_INDEX, "high_quality_stablecoin": CAIP_USDC}
    draft = build_draft(MIRIAM_CORE, resolved_assets=resolved)
    by_class = {w.asset_class: w.asset_id for w in draft.weights}
    assert by_class["broad_equity_index"] == CAIP_INDEX
    assert by_class["high_quality_stablecoin"] == CAIP_USDC
    # Classes with no resolved id stay empty rather than being filled in.
    assert by_class["tokenized_real_world_assets"] == ""


def test_offline_is_the_default_without_a_key():
    client = GliderClient(api_key="", transport=httpx.MockTransport(lambda r: ok({})))
    assert client.online is False
    assert client.configured is False


def test_validate_draft_offline_says_it_was_not_submitted():
    """No key means the draft is still built, and nothing leaves the machine."""
    client = GliderClient(api_key="", transport=httpx.MockTransport(lambda r: ok({})))
    draft = build_draft(MIRIAM_CORE)
    result = _run(client.validate_draft(draft))
    assert result.status == "draft_local"
    assert result.submitted is False
    assert "not submitted" in result.note


def test_validate_draft_with_a_key_but_no_ids_stays_local():
    client = client_for(lambda r: ok({"valid": True}))
    draft = build_draft(MIRIAM_CORE)  # no ids resolved
    result = _run(client.validate_draft(draft))
    assert result.status == "draft_local"
    assert "asset ids resolved" in result.note


def test_validate_draft_posts_a_real_payload_when_ids_resolve():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return ok({"valid": True})

    client = client_for(handler)
    resolved = {
        "broad_equity_index": CAIP_INDEX,
        "tokenized_real_world_assets": CAIP_RWA,
        "high_quality_stablecoin": CAIP_USDC,
        "tokenized_short_duration_paper": CAIP_PAPER,
    }
    result = _run(
        client.validate_draft(build_draft(MIRIAM_CORE, resolved_assets=resolved))
    )

    assert result.status == "validated"
    assert result.submitted is False
    assert seen and seen[0].url.path.endswith("/strategies/validate")
    body = json.loads(seen[0].content)
    weights = [row["weight"] for row in body["allocation"]["assets"]]
    assert sum(Decimal(w) for w in weights) == Decimal("100")


def test_a_glider_rejection_is_an_answer_not_an_exception():
    def handler(request):
        return err(400, "API_400", "Asset(s) are not recognized", ["0xdead"])

    client = client_for(handler)
    resolved = {
        "broad_equity_index": CAIP_INDEX,
        "tokenized_real_world_assets": CAIP_RWA,
        "high_quality_stablecoin": CAIP_USDC,
        "tokenized_short_duration_paper": CAIP_PAPER,
    }
    result = _run(
        client.validate_draft(build_draft(MIRIAM_CORE, resolved_assets=resolved))
    )
    assert result.status == "rejected"
    assert result.submitted is False
    assert "API_400" in result.note


def test_drafts_are_never_submitted_by_the_client():
    """The draft path calls validate, never create and never enroll."""
    import inspect

    from miriam_agent.integrations import glider_client as module

    source = inspect.getsource(module.GliderClient.validate_draft)
    assert "validate_strategy" in source
    assert "create_strategy" not in source
    # No route literal and no method call, so a note that merely says the word
    # "enrollment" does not trip this.
    assert '"/enroll' not in source
    assert "self.prepare_enrollment(" not in source
    assert "await self.create_strategy(" not in source
