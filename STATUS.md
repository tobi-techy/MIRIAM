# STATUS: Miriam money path

Last verified: 2026-09-21. Run the gates with:
`uv run python -m pytest tests/test_hands_layer.py tests/test_orchestrator_layer.py tests/test_money_response_contract.py`

## Where the money code lives

| Concern | Path | State |
| --- | --- | --- |
| Hands (ledger, limits, transfer, audit) | `miriam_agent/hands/` | **Wired.** All money execution is deterministic Python. |
| Judgment | `miriam_agent/judgment/decide.py` | **Wired.** Types decisions; never moves money. |
| Orchestrator | `miriam_agent/orchestrator.py` | **Wired.** Single entrypoint: Hands → Judgment → Hands → Voice. |
| Voice | `miriam_agent/voice/generate.py` | **Wired.** Narrates STATE, clamps figures, prints challenge fields. |
| Go rail (Go is the money authority) | `miriam_agent/integrations/go_client.py` | **Wired.** `GoRail` sends `X-Miriam-Confirm-Id` / `X-Miriam-Receipt-Id` and receipts persist them. |
| Inflow | `miriam_agent/hands/split.py` | **Wired.** `POST /api/v1/money/inflow` splits; 503 = ledger down, caller retries; 4xx = bad request. |
| Confirm settle | `orchestrator._handle_confirm` | **Wired.** `{confirm_id, yes}` only; a bare "yes" does not settle; `yes=false` declines. |

## The one math door

```python
build_money_plan(user_profile, accounts=None, glider_state=None, *, reference=None, today=None) -> MoneyPlan
```

`user_profile` is an `IntakeProfile`, `FinancialProfile`, onboarding state, or a dict. `accounts` are connected balances (they outrank anything stated by hand). `glider_state` is an existing portfolio, which turns a draft into a monitor decision.

`explain_money_plan(...)` returns the same run as a reasoning trace, for tests and support. It delegates to the same internal core, so the two views cannot disagree. There is no second planner: `run_pipeline` was folded in and removed.

## Wired / dead

- **Wired:** `get_money_plan` is registered and reachable from any turn; the operator prompt requires it before a money claim; `money/agent.py` clamps and gates narration; the demo and the `money-plan` console script produce a plan; 252 money tests cover the rules.
- **Dead / not yet wired:** the money package is still not *called* by `api/chat.py` on the normal path, because the prompt tells the model to call the tool rather than the server pre-computing the plan. That is the intended wiring for this pass.
- **Not in the repo:** `POST /agent/money/plan`. The CLI is the product path; the route is a thin wrapper nobody has needed yet.

## Soft spots that remain

1. `money/reference.py` figures are **PLACEHOLDER**, not sourced. Dated (`2026-09-19`) and labelled everywhere, and past 90 days the pipeline refuses to invest. Replace with real figures before advising anyone.
2. Asset ids are never resolved, because nothing in this repo knows the live Glider asset catalogue. Drafts are correct on weights and say the ids are missing.
3. Connected balances come from `go_client.get_financial_health` (income, cash). Fixed costs still have to come from the user, so a ledger-only user gets a `data_gap` asking for them.
