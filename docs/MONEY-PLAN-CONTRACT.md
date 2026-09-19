# MIRIAM Money Plan Contract

Status: canonical output contract for `miriam_agent/money/`.
Companion to `docs/MONEY-RULES.md` (the rules) — this document is the *shape*
of what comes out.

**Invariant:** every user-facing money answer is a `MoneyPlan` instance,
rendered. The LLM never constructs one; Python does. See `money/schema.py`.

---

## 1. Scope

Miriam's money pipeline is **read, diagnose, recommend**. It does not move money.

- Money execution lives in RAIL_BACKEND (Go). Nothing here bypasses it.
- `GliderAction` is a recommendation plus a *validated draft*.
- Enrollment and withdrawals are user-signed and two-stage. Miriam prepares,
  explains, and hands off. She never auto-enrolls.

---

## 2. The pipeline

```
intake.py  →  IntakeProfile            normalize, never invent
diagnose.py → Diagnosis                one blunt sentence, first
safety.py  →  SafetyStack              the ordered gate, investing_allowed
cashflow.py → CashflowSplit            fixed / save / invest / guilt-free
allocation.py → AllocationBook         70/30 + override + reason
glider.py  →  GliderAction             none | recommend | monitor
plan.py    →  MoneyPlan                assemble + render, no LLM
agent.py   →  MoneyPlan                LLM writes words, then gets clamped
```

Money is `Decimal` in the schema and converted only at the boundary. The
existing audit flagged float money as a defect; the new surface does not repeat it.

---

## 3. Schema

```python
class MoneyPlan(BaseModel):
    diagnosis: str                    # the blunt sentence, 1-2 lines
    problem_type: ProblemType         # the 9-way Literal
    currency: str

    monthly_take_home: Decimal
    cashflow: CashflowSplit
    buffer: BufferPlan
    debts: list[DebtAction]
    surplus_monthly: Decimal          # investable. 0 when gated.
    book: AllocationBook              # 70/30 or override, + why
    glider: GliderAction

    actions_90d: list[Action]         # this week / this payday / this month
    automation_rules: list[str]       # what moves on payday without thinking
    kill_switches: list[str]          # when to pause / cut / raise

    assumptions: list[str]            # every ASSUMED label
    confidence: Literal["high", "medium", "low"]
    disclaimer: str                   # non-optional, on every instance
    what_would_change: list[str]
```

### Sub-models

| Model | Fields |
|---|---|
| `CashflowSplit` | `fixed`, `savings`, `investments`, `guilt_free` (Decimal), `currency`, `shares` (dict), `crisis` (bool), `note` |
| `BufferPlan` | `target_months`, `target_amount`, `current_amount`, `gap`, `months_to_fill`, `vehicle`, `location`, `is_glider: False` |
| `DebtAction` | `label`, `balance`, `apr_pct`, `minimum_monthly`, `extra_monthly`, `band` (fire/judgment/keep), `strategy` (snowball/avalanche), `reason` |
| `AllocationBook` | `growth_pct`, `defensive_pct`, `rule_id`, `overrides` (list), `reason`, `growth_sleeve` (list), `defensive_sleeve` (list) |
| `GliderAction` | `kind` (none/draft/monitor), `template`, `strategy_id`, `portfolio_id`, `draft` (`GliderDraft`), `validation`, `drift`, `risks` (list), `blocked_reason` |
| `GliderDraft` | `name`, `template`, `book`, `weights` (list of `DraftWeight`), `schedule`, `status` (draft_local/validated/rejected), `submitted` (pinned false), `note` |
| `Action` | `when`, `what`, `amount`, `currency`, `how` |

Money fields are `Decimal`. `surplus_monthly` is the **investable** surplus —
money that cleared the safety stack *and* has a 5+ year horizon. It is `0` when
`investing_allowed` is false, and that is structural, not advisory.

---

## 4. Rendered output (12 parts, always this order)

1. **Diagnosis** — the blunt sentence. No preamble.
2. **Reality check** — what the numbers actually say.
3. **90-day plan** — this week / this payday / this month.
4. **Cashflow split** — the table, in currency.
5. **Debt actions** — if any.
6. **Buffer target and current gap.**
7. **Investable surplus and the 70/30 book.**
8. **Glider action** — none | recommend strategy X | monitor existing portfolio.
9. **Automation rules** — what moves on payday without thinking.
10. **Kill switches** — when to pause investing, cut spend, raise income.
11. **Assumptions and confidence** — high / medium / low.
12. **What would change this plan.**

Tone: senior operator. Direct, specific amounts, specific dates. No TED talk,
no encouragement, no "you got this".

---

## 5. Glider mapping

Glider's model, as the API actually defines it:

- A **strategy** is a reusable template: an allocation, a schedule, swap
  preferences. One allocation version at a time.
- A **portfolio** connects one user to a strategy, with one smart account per
  chain. All enrolled portfolios follow their strategy's allocation and schedule.
- **Non-custodial.** The user funds it. Glider does not hold the money.
- Two rebalance triggers: **scheduled** (strategy `frequency`; read `nextDueAt`
  and `lastRebalanceAt` from the portfolio `schedule`) and **manual**
  (`POST /v2/portfolios/{id}/rebalance`, subject to a per-portfolio cooldown).
- Allocation: 1–50 assets, CAIP-19 `assetId`, `weight` as a **string percent
  summing to 100, max 2 decimals**.
- Async work returns `202` + `operationId`. Poll
  `GET /v2/portfolios/{id}/operations/{opId}` every 2–5s until `completed`,
  `failed` or `cancelled`. Never assume instant settlement.
- API base `https://api.glider.fi/v2`, `x-api-key` header. Envelope
  `{success, data}`, errors `{success: false, error: {code, message, details}}`.

### Preconditions (all must hold)

Miriam may recommend a Glider strategy **only if**:

1. the safety stack is complete, or the user explicitly waived it after a warning;
2. the surplus can be left untouched through a 40% drawdown (`R-HOUSEL-1`);
3. the user can self-custody and complete the two-stage, user-signed enrollment;
4. the strategy weights match the approved book;
5. assets validate via `POST /v2/strategies/validate`;
6. the user has been told: smart contract risk, stablecoin depeg risk, no
   deposit insurance, and that they can lose principal.

### Never

- Never put emergency funds in a Glider strategy.
- Never auto-enroll. Recommend → explain → user signs.
- Never guess an asset ID. Resolve it, or fail.
- Never quote a yield, APY, or performance figure that did not come from the API.

### What a draft actually contains

`GliderAction.kind == "draft"` carries a complete, inspectable proposal:

- `name` and `book` (e.g. "Miriam Core 70/30")
- `weights`: percent values that **sum to exactly 100**, ordered growth first
- `schedule`: the rebalance cadence Glider stores on the strategy
- `status`, one of:
  - `draft_local` — built and inspectable, **not submitted**. This is the answer
    whenever `GLIDER_API_KEY` is unset, and the note says so.
  - `validated` — Glider accepted it on a `POST /v2/strategies/validate` dry run.
    Still not submitted; enrollment remains user-signed.
  - `rejected` — Glider refused the allocation, with its reason in `note`.
- `submitted`: pinned to `false` by the schema. A submitted draft is not a
  representable state, so no code path can produce one.

Asset ids stay empty until a real source supplies them (the user's own live
positions). `as_payload()` refuses to build a submit-ready body while any id is
missing, rather than filling one in.

`kind == "none"` always carries a `blocked_reason`. The schema rejects a refusal
without one, so "no" is never unexplained.

---

## 6. Strategy templates

Three books. Assets are chosen **per supported chain at runtime** and validated —
never hardcoded addresses, which go stale and can point at dead contracts.

| Template | Book | For |
|---|---|---|
| **Miriam Core** | 70 / 30 | Horizon 7y+, stable income, safety stack complete. The default. |
| **Miriam Preserve** | 40 / 60 | Horizon 3–7y, or low capacity (single income, dependents, volatile pay). |
| **Miriam Build** | 80 / 20 | Horizon 15y+, high capacity, and a recorded acceptance of a 40% drawdown. |

Each template carries: name, objective, risk, horizon, a sleeve spec of asset
*classes* (not addresses), a rebalance schedule, and swap preferences.

**Sleeves.**

- *Growth*: broad equity index, or a target-date equivalent. On the Glider path,
  a diversified onchain book mapped to the same 70/30 — never a single memecoin,
  never a single asset standing in for a sleeve (`R-BOGLE-2`).
- *Defensive*: cash, short government paper, quality money market. High-quality
  stablecoins only if the user already lives onchain **and** understands depeg
  risk. A volatile token is never labelled defensive.

---

## 7. Mandatory disclosures

Present on every plan, not optional:

- Miriam is not a licensed financial adviser.
- No deposit insurance on the onchain path.
- Smart contract risk, liquidity risk, depeg risk, chain risk, key-management
  risk, and behaviour risk.
- Principal can be lost.
- Every assumption is labelled, with the reference table's as-of date.

Plus a `what_would_change` list — the specific, checkable things that would make
this a different plan (income change, debt cleared, buffer reached, horizon
moved, rates moved).

### Local reference figures

The local inflation and risk-free rates in `money/reference.py` carry an as-of
date and a `sourced` / PLACEHOLDER label. The rules:

- Every row is dated. `REFERENCE_AS_OF` is the day the table was pinned, not a
  claim that the figures are live.
- An unsourced row is labelled PLACEHOLDER in the plan's own `assumptions`, so a
  hand-entered figure never reads as a sourced one.
- Past **90 days** the row is treated as unknown: `investing_allowed` becomes
  `false` with a stated reason, and plan confidence drops to `low`. A stale
  hurdle rate is worse than no hurdle rate, because it still produces a
  confident answer.
- A country with no row falls back to the global settings thresholds and is
  flagged as unmatched.

---

## 8. Verification

- The ordering invariant is tested: no fixture may receive an investment
  recommendation before the buffer and debt rules fire.
- The Glider client is tested against `httpx.MockTransport` and never a live API.
- The narration clamp is tested adversarially: prose containing a number the
  pipeline did not compute must be rejected.
