# Miriam Onboarding — Architecture & Diff Plan

Status: approved for incremental implementation.
Scope: rebuild Rail's onboarding around Miriam as a *financial intelligence system
that happens to communicate through chat* — not a chatbot on a fintech app, and
not a KYC form with a conversation stapled on.

---

## 1. The product idea, in one line

> Your money needs a default.

Miriam moves the user through:

```
curiosity -> conversation -> understanding -> diagnosis -> first aha
-> personal plan -> "Rail executes this" -> investing (when earned)
-> readiness -> Glider execution -> automation
```

## 2. What already exists (do not rebuild)

| Layer | Where | State |
|---|---|---|
| Conversational onboarding (LLM-led, deterministic machine) | `miriam_agent/onboarding/` | **Built.** `service.py` (state machine, `_TRANSITIONS`), `driver.py` (LLM conductor + structured tool output), `plan.py` (deterministic Financial State Model), `contracts.py` (pydantic), `state.py` (Redis + migrations), `trace.py`, `quality.py`, `evals.py` |
| Ledger-backed health / forecast / plan | `financial/intelligence.py` | **Built** from Go `analytics/financial-snapshot` |
| Ranked structural-cause engine | `financial/hypotheses.py` | **Built** (leak categories, probes) |
| Aha detection | `utils/aha.py` | **Built** (recognition/insight/reframe/relief) |
| Safety / policy / audit / rate limit | `safety/` | **Built** |
| Go backend client (incl. **full Glider investment API**) | `integrations/go_client.py` | **Built**: `limits`, `portfolio`, `assets`, `strategies`, `executions`, `enroll`, `orders`, `allocations`, `pause/resume/rebalance` |
| Agent tool registry (23 Glider tools, staged confirmation) | `tools/investment_definitions.py` | **Built** |
| Long-term memory | `integrations/supermemory_client.py` + `conversational/` | **Built** |
| Short-term memory | `database/working_memory.py` (Redis) | **Built** |
| Proactive analyst | `proactive/` | **Built** |
| Observability | `observability/` | **Built** (partial funnel) |
| API | `api/chat.py`, `api/proactive.py` | **Built** |

**Consequence:** the spec's §31 ("do not duplicate Go infrastructure") and §36
("inspect first") are already honoured. This plan is *additive*.

## 3. Architectural rule (§2) — how it is enforced today and after

The LLM is never the source of truth. The existing split already implements this
and is preserved:

| Concern | Owner | Enforcement |
|---|---|---|
| Onboarding state / stage | Python `onboarding/service.py` `_TRANSITIONS` | Table lookup; unknown `(stage,intent)` -> stay |
| Extracted info | LLM proposes -> `driver._clean_facts` bounds -> service persists | Typed `ConductorOutcome`, `extra="forbid"` |
| Financial calculations | **deterministic engines only** | New `financial/*` modules; LLM only narrates |
| KYC / account / wallet | **Go backend** | Python reads, never writes |
| Investment eligibility / limits | **Go `/investments/limits`** + new eligibility engine | `financial/eligibility.py` |
| Transaction authorization | Go (staged `confirmation_token`) | Action layer + `investment_intents.py` |
| Compliance | Go + `safety/policy.py` | Policy verdicts |

## 4. Target component map

```
Miriam Python Service
|- Conversation Engine        -> onboarding/driver.py + onboarding/quality.py   [exists]
|- Onboarding Orchestrator    -> onboarding/service.py + onboarding/state.py    [exists, EXTEND]
|- Financial Intelligence     -> financial/intelligence.py                      [exists]
|   |- Structured profile     -> financial/profile.py                           [NEW]
|   |- Diagnosis engine       -> financial/diagnosis.py                         [NEW]
|   \- Allocation/plan engine -> financial/allocation.py                        [NEW]
|- Investment Intelligence    -> financial/readiness.py                         [NEW]
|- Policy / Eligibility       -> financial/eligibility.py                       [NEW]
|- Action Layer (provider)    -> investments/provider.py                        [NEW]
|   \- Intent lifecycle       -> investments/intents.py                         [NEW]
|- Memory                     -> database/working_memory + supermemory + facts   [exists]
\- API                        -> api/miriam.py                                  [NEW]
```

## 5. Diff plan (incremental, each step ships green)

### Phase A — deterministic intelligence engines (pure, testable)
New modules, **zero changes to existing behaviour**:

- `financial/profile.py` — `FinancialFact{value, source, confidence, updated_at,
  currency}` + `FinancialProfile` (§7 field list) with a **merge policy**
  (user correction > higher confidence > higher recency), plus
  `extract_money_facts(text)` for §6 natural extraction
  (`"500k"`, `"half a million"`, `"NGN 500,000/month"`, `"i spend maybe 350k"`)
  and `profile_from_onboarding_state(state)` to bridge existing free-form facts.
- `financial/diagnosis.py` — §9 ranked diagnosis: `primary_problem`,
  `secondary_problems[]`, `confidence`, `evidence[]`, `priorities[]` over the
  §9 problem vocabulary; consumes the profile (+ optional ledger snapshot) and
  reuses `hypotheses.rank_hypotheses` rather than duplicating it.
- `financial/allocation.py` — §11 `Everyday / Safety / Future / Flexible` in
  **currency amounts**, derived from income, essentials, debt, buffer, income
  volatility and goal horizon. Never fixed percentages. Never exceeds surplus.
  Produces `assumptions[]` + `confidence` so estimates are labelled, never
  fabricated.
- `financial/readiness.py` — §15 `NOT_READY | BUILD_SAFETY_FIRST |
  READY_TO_START | READY_TO_AUTOMATE | REVIEW_REQUIRED` + reason +
  `recommended_next_step`. LLM cannot override.
- `financial/eligibility.py` — §18 policy verdicts for an investment action
  (KYC tier, amount vs. limits, min cash reserve, jurisdiction, unsupported
  asset). **Consumes Go's real `/api/v1/investments/limits`**; no invented limits.

### Phase B — Action Layer / Glider (§16–18)
- `investments/provider.py` — `InvestmentProvider` ABC
  (`create_portfolio`, `get_portfolio`, `get_assets`, `create_order`,
  `get_order`, `cancel_order`, `get_positions`, `get_balance`) +
  `GliderInvestmentProvider` (thin adapter over the **existing** `go_client`
  methods — no invented endpoints) + `MockInvestmentProvider`.
  `cancel_order` is **declared unsupported by Glider** (no such endpoint exists)
  instead of being faked; capability flags make this explicit.
- `investments/intents.py` — §17–18 lifecycle: resolve user -> balance ->
  eligibility -> policy -> limits -> amount -> **summary -> explicit confirmation
  -> execute -> monitor -> memory -> notify**. Every intent carries an
  `idempotency_key`, state (`DRAFT -> AWAITING_CONFIRMATION -> EXECUTING ->
  SETTLED | FAILED | CANCELLED`), provider reference, retry policy and audit
  trail. Duplicate/out-of-order provider events are absorbed.

### Phase C — lifecycle stages & resume (§4, §20)
Extend `onboarding/state.py` with the **wealth lifecycle** vocabulary
(`ONBOARDING_NOT_STARTED -> DISCOVERY -> FINANCIAL_CONTEXT -> CASHFLOW_DISCOVERY
-> FINANCIAL_DIAGNOSIS -> FIRST_AHA -> GOAL_DISCOVERY -> FINANCIAL_PLAN ->
RAIL_VALUE -> INVESTMENT_DISCOVERY -> INVESTMENT_READINESS -> ACTION_SELECTION ->
ACCOUNT_SETUP -> KYC -> FUNDING -> INVESTMENT -> ONBOARDING_COMPLETE`) plus a
deterministic mapping from the existing conversational stages, and a resume
payload (`current_stage`, `completed_stages`, `unresolved`,
`next_recommended_step`). The existing six stages keep working unchanged
(back-compatible migration).

### Phase D — API + observability (§25, §30)
- `api/miriam.py` — `/miriam/message`, `/miriam/onboarding/{start,state,resume}`,
  `/miriam/financial-profile`, `/miriam/financial-diagnosis`,
  `/miriam/financial-plan`, `/miriam/investment-readiness`,
  `/miriam/investment/{intent,confirm,execute,status}`.
  Financial-execution routes are **separate** from conversational routes.
- `observability/metrics.py` — add the §25 funnel counters (time-to-aha,
  stage abandonment, recovery).

## 6. Explicit non-goals / preserved behaviour

- `onboarding/service.py`'s existing conversational arc is **not** rewritten.
- `plan.py`'s Financial State Model stays the conversational plan narrator; the
  new `allocation.py` adds *amounts*, it does not replace it.
- No second user identity system (§31). No money movement outside Go.
- No KYC collected conversationally (§19) — Go owns the flow.
- No fabricated numbers anywhere: every engine labels estimates and confidence
  (§10, §29).

## 7. Definition of done mapping

The §35 checklist is satisfied structurally by Phases A–D for the intelligence
and execution layers (1–13, 18–21). Steps 14–17 (account activation, KYC,
funding, portfolio connect) are Go-owned flows that Miriam *orchestrates and
narrates*; the Python side exposes them as lifecycle stages + readiness gates
rather than re-implementing them.