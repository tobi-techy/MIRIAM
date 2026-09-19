# STATUS: Miriam money path

Last verified: 2026-09-19 (branch `tobi-techy/merrow`). Run the gates with:
`uv run python -m miriam_agent.money.demo --all`

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
| Money *execution* | `RAIL_BACKEND` (Go), via `integrations/go_client.py` | Out of scope. Python never moves money |

## The one math door

```python
build_money_plan(user_profile, accounts=None, glider_state=None, *, reference=None, today=None) -> MoneyPlan
```

`user_profile` is an `IntakeProfile`, `FinancialProfile`, onboarding state, or a
dict. `accounts` are connected balances (they outrank anything stated by hand).
`glider_state` is an existing portfolio, which turns a draft into a monitor
decision.

`explain_money_plan(...)` returns the same run as a reasoning trace, for tests
and support. It delegates to the same internal core, so the two views cannot
disagree. There is no second planner: `run_pipeline` was folded in and removed.

## Wired / dead

- **Wired:** `get_money_plan` is registered and reachable from any turn; the
  operator prompt requires it before a money claim; `money/agent.py` clamps and
  gates narration; the demo and the `money-plan` console script produce a plan;
  252 money tests cover the rules.
- **Dead / not yet wired:** the money package is still not *called* by
  `api/chat.py` on the normal path, because the prompt tells the model to call
  the tool rather than the server pre-computing the plan. That is the intended
  wiring for this pass.
- **Not in the repo:** `POST /agent/money/plan`. The CLI is the product path; the
  route is a thin wrapper nobody has needed yet.

## Soft spots that remain

1. `money/reference.py` figures are **PLACEHOLDER**, not sourced. Dated
   (`2026-09-19`) and labelled everywhere, and past 90 days the pipeline refuses
   to invest. Replace with real figures before advising anyone.
2. Asset ids are never resolved, because nothing in this repo knows the live
   Glider asset catalogue. Drafts are correct on weights and say the ids are
   missing.
3. Connected balances come from `go_client.get_financial_health` (income, cash).
   Fixed costs still have to come from the user, so a ledger-only user gets a
   `data_gap` asking for them.
