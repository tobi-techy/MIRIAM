# STATUS: Miriam money path

Last verified: 2026-09-21. Run the gates with:
`uv run python -m pytest tests/test_hands_layer.py tests/test_orchestrator_layer.py tests/test_money_response_contract.py`

## Where the money code lives

| Concern | Path | State |
| --- | --- | --- |
| Money maths (planning) | `miriam_agent/money/` | **Wired.** `intake` `diagnose` `safety` `cashflow` `allocation` `reference` `templates` `glider` `schema` `plan` `agent` `text` `formatting` `demo` |
| Glider wire | `miriam_agent/integrations/glider_client.py` | **Wired.** Reads, validate, drafts, offline mode. No stage-2 enroll, no withdraw |
| Household envelopes | `miriam_agent/financial/allocation.py` | Built, **not** a 70/30 (everyday/safety/debt/future/flexible). Different job. Do not merge |
| Spec diagnosis | `miriam_agent/financial/diagnosis.py` | Built, 14 spec problems. Parallel to `money/diagnose.py` (9 pipeline problems), different vocabularies by design |
| User-facing agent | `miriam_agent/agents/agent_loop.py`, `api/chat.py` | Built. `POST /api/v1/chat`, `/chat/stream` |
| System prompt | `miriam_agent/agents/system_prompt.py` | **Wired.** Operator-first `MONEY OPERATOR RULES` block inside `BASE_PROMPT` |
| Tool registry | `miriam_agent/tools/money_definitions.py` | **Wired.** `get_money_plan`, read-only, auto-execute |
| Vault tools | `miriam_agent/tools/vault_definitions.py` | **Wired.** `get_vault_context`, `preview_plan`, `preview_withdraw`, `propose_vault_plan` (staged), read-only, auto-execute. Copy-guarded (`money/vault_copy.py`) |
| Vault wire | `miriam_agent/integrations/go_client.py` | **Wired.** Reads only: `get_vault`, `list_vault_strategies`, `get_vault_activity`, `preview_vault_withdraw`. No vault POST, no withdraw-submit |
| Vault voice | `miriam_agent/agents/system_prompt.py`, `miriam_agent/voice/prompt.py` | **Wired.** Locked dollar block: Rail-owned tiers, four facts, Go preview before exit numbers |
| Hands (ledger, limits, transfer, audit) | `miriam_agent/hands/` | **Wired.** All money execution is deterministic Python. |
| Judgment | `miriam_agent/judgment/decide.py` | **Wired.** Types decisions; never moves money. |
| Orchestrator | `miriam_agent/orchestrator.py` | **Wired.** Single entrypoint: Hands → Judgment → Hands → Voice. |
| Voice | `miriam_agent/voice/generate.py` | **Wired.** Narrates STATE, clamps figures, prints challenge fields. |
| Go rail (Go is the money authority) | `miriam_agent/integrations/go_client.py` | **Wired.** `GoRail` sends `X-Miriam-Confirm-Id` / `X-Miriam-Receipt-Id` and receipts persist them. |
| Inflow | `miriam_agent/hands/split.py` | **Wired.** `POST /api/v1/money/inflow` splits; 503 = ledger down, caller retries; 4xx = bad request. |
| Confirm settle | `orchestrator._handle_confirm` | **Wired.** `{confirm_id, yes}` only; a bare "yes" does not settle; `yes=false` declines. |
| Money *execution* | `RAIL_BACKEND` (Go), via `integrations/go_client.py` | Out of scope. Python never moves money |

## The one math door

```python
build_money_plan(user_profile, accounts=None, glider_state=None, *, reference=None, today=None) -> MoneyPlan
```

`user_profile` is an `IntakeProfile`, `FinancialProfile`, onboarding state, or a dict. `accounts` are connected balances (they outrank anything stated by hand). `glider_state` is an existing portfolio, which turns a draft into a monitor decision.

`explain_money_plan(...)` returns the same run as a reasoning trace, for tests and support. It delegates to the same internal core, so the two views cannot disagree. There is no second planner: `run_pipeline` was folded in and removed.

When Go reports an active vault, the planner treats the locked dollar sleeve
as the long-horizon book: `glider.kind` is `none`, no second Miriam draft is
emitted on top, and surplus/automation lines name the vault percent and unlock
date. Vault tiers (Steady/Balanced/Growth) are Rail labels, not planner books;
the mix stays in the Rail YAML. Vault execution is the app via Go confirm +
passcode; Python stages the draft and never POSTs.

## Wired / dead

- **Wired:** `get_money_plan` is registered and reachable from any turn; the operator prompt requires it before a money claim; `money/agent.py` clamps and gates narration; the demo and the `money-plan` console script produce a plan; 252 money tests cover the rules.
- **Dead / not yet wired:** the money package is still not *called* by `api/chat.py` on the normal path, because the prompt tells the model to call the tool rather than the server pre-computing the plan. That is the intended wiring for this pass.
- **Not in the repo:** `POST /agent/money/plan`. The CLI is the product path; the route is a thin wrapper nobody has needed yet.

## Soft spots that remain

1. `money/reference.py` figures are **PLACEHOLDER**, not sourced. Dated (`2026-09-19`) and labelled everywhere, and past 90 days the pipeline refuses to invest. Replace with real figures before advising anyone.
2. Asset ids are never resolved, because nothing in this repo knows the live Glider asset catalogue. Drafts are correct on weights and say the ids are missing.
3. Connected balances come from `go_client.get_financial_health` (income, cash). Fixed costs still have to come from the user, so a ledger-only user gets a `data_gap` asking for them.
