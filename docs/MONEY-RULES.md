# MIRIAM Money Rules

Status: canonical rule source for `miriam_agent/money/`.
Every rule the pipeline applies is listed here with its trigger, action, kill
condition and overridable numbers. Code cites these IDs (`R-*`) in comments and
in plan `assumptions`, so a user-facing claim always traces back to a rule.

**Reading the table.** *Trigger* is the condition that makes the rule apply.
*Action* is what the pipeline does. *Kill* is when the rule must be ignored —
a rule with no kill condition is a slogan, not a rule. *Numbers* are defaults
with the override path.

---

## 1. The prime directive

Auditor posture, borrowed from bank reconciliation. Money is accounted for in
strict order, and nothing is allocated before it reconciles:

```
income in  →  leaks out  →  buffer  →  liabilities  →  surplus
```

If a stage does not reconcile, the pipeline stops there and says so. It does
not skip ahead to a prettier stage. This single ordering is what makes the
safety stack non-negotiable: surplus literally does not exist until the stages
above it are satisfied.

---

## 2. Rule table

### Ramit Sethi — Conscious Spending Plan

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-SETHI-1` | Always, once income is known | Split take-home into fixed costs / savings / investments / guilt-free | Never — this is the plan's skeleton | Fixed 50–60, Invest 10–20, Save 5–10, Guilt-free 20–30 (% of take-home) |
| `R-SETHI-2` | Guilt-free would be 0 | Force a non-zero guilt-free line | Only when `problem_type` is `cashflow_bleed` or `high_interest_debt` | Minimum 5% of take-home |
| `R-SETHI-3` | Fixed costs > 60% of take-home | Say plainly: this is a cost-cut or income-raise problem, not a pie-chart problem | User's fixed costs are genuinely fixed (rent, school fees) and income is the only lever | 60% ceiling |
| `R-SETHI-4` | User can pay for a plan that is 85% as good, but the perfect plan needs effort they will not spend | Take the 85% plan | The extra 15% is load-bearing (e.g. the 15% is the only thing covering a legal obligation) | — |
| `R-SETHI-5` | Any recommendation would require the user to keep a spreadsheet | Automate it or drop it | User explicitly asks to track manually | — |

Kill rationale for `R-SETHI-2`: a 0% fun plan gets abandoned, and an abandoned
plan protects nothing. Crisis is the only exception, and crisis is a state we
diagnose, not one the user declares.

### Morgan Housel — behaviour and staying wealthy

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-HOUSEL-1` | Any plan is about to be recommended | Check it survives a 40% drawdown without the user selling | Never | 40% drawdown assumption |
| `R-HOUSEL-2` | Investable surplus is being sized | Only money untouched for 12–24 months is investable | Never — hard product rule | 12–24 month floor |
| `R-HOUSEL-3` | User describes wanting to get rich quickly, or to time the market | Classify `overconfidence_risk`; cap growth sleeve | Chart/figure evidence of a multi-year track record and genuinely long horizon | Growth cap 50–60% |
| `R-HOUSEL-4` | Defensive sleeve is being sized | Size it for *staying* wealthy, not for maximising return | Horizon ≥ 15y **and** high capacity **and** explicit drawdown acceptance | See `R-BACH-2` table |
| `R-HOUSEL-5` | Volatile income | Raise the buffer, lower the growth cap | Income is steady and verified | Buffer 6 months, growth cap 60% |

### Bogle / Bogleheads / JL Collins — the growth sleeve

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-BOGLE-1` | Growth sleeve is being built | Use low-cost broad exposure — broad equity index, or a target-date equivalent | No broad index or equivalent is accessible in the user's country/currency | — |
| `R-BOGLE-2` | User wants to hold a single asset | Refuse as a *sleeve*; a single name is not diversification | The single asset is a broad index tracker | — |
| `R-BOGLE-3` | Markets move | Do not react. No timing, no tactical shifts | The user's horizon or capacity has structurally changed | — |
| `R-BOGLE-4` | Suggestions to add complexity, overlays, or leverage appear | Reject | Never | — |

### Dave Ramsey — starter buffer and debt firefighting only

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-RAMSEY-1` | Debt carries an APR at or above the fire threshold | Starter buffer = **1 month** only, then attack debt | No fire-rate debt | 1 month essentials |
| `R-RAMSEY-2` | Debt exists | Pay minimums everywhere, direct the surplus at one debt | A lower-APR debt is the only debt and cash is already adequate | — |
| `R-RAMSEY-3` | Debt is lowest-balance-first vs highest-APR-first | Snowball (smallest balance) when the user needs momentum; avalanche (highest APR) when the maths matters | Never both at once — pick and state why | — |

**Explicitly rejected from Ramsey:** his investing arm and his return
assumptions. We do not use an 8% growth assumption, and we do not tell anyone
their returns are guaranteed. `R-RAMSEY-1` and `R-RAMSEY-2` are the only
things borrowed.

### David Bach — automate, and the 70/30 book

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-BACH-1` | Income is steady and a surplus exists | Automate on payday, before it can be spent | Income is irregular — automate a trigger, not a date | — |
| `R-BACH-2` | Investable surplus has cleared the safety stack | Apply the 70/30 book, adjusted by horizon and capacity | Horizon < 3 years | See table below |
| `R-BACH-3` | Book is being chosen | Record the reason for every override | Never — an unexplained override is a bug | — |

#### `R-BACH-2` override table (growth / defensive)

| Condition | Book |
|---|---|
| `investing_allowed` is false | 0 / 100 — cash-like only. No book. |
| Horizon < 3 years | No 70/30. Cash-like instruments only. |
| Horizon 3–7 years | 50/50 → 60/40 |
| Horizon 7+ years, stable income | **70/30 default** |
| Horizon 15+ years, high capacity | 80/20 max, only on explicit drawdown acceptance |
| Low capacity (single income, dependents, volatile pay) | Growth capped 50–60 even if the user feels aggressive |

Precedence is top-to-bottom; the first matching row wins. The low-capacity cap
is applied *last* and can lower a book the rows above chose.

### Auditor posture — provenance and reconciliation

| ID | Trigger | Action | Kill | Numbers |
|---|---|---|---|---|
| `R-AUDIT-1` | A number is missing | Ask for it, or mark `ASSUMED` and lower confidence | Never invent it | — |
| `R-AUDIT-2` | A rate, yield or return is needed | Use user data or the curated reference table, labelled with its as-of date | Never quote an inferred figure as fact | `reference.py`, every read returns `assumed: bool` |
| `R-AUDIT-3` | A plan is rendered | Include the disclaimer and the "what would change this plan" list | Never | — |
| `R-AUDIT-4` | The user's country/currency is known but the advice being adapted is from another market | Adapt the wrapper, not just the number. US 401(k)/Roth advice is invalid for a Lagos cash earner | The user genuinely has access to that wrapper | — |

---

## 3. Safety stack (Stage 2) — the ordered gate

This is the one part of the pipeline that cannot be reordered. It is the
mechanism behind every other rule.

```
1. Stop the bleed.      spend > income                      → investing forbidden
2. Buffer.              target = 1 month if debt is on fire
                        else 3 months steady / 6 volatile
                        vehicle from reference.py, NEVER Glider
3. Debt triage.         APR >= 15%        → fire, attack it
                        8% <= APR < 15%   → judge vs local risk-free rate
                        APR < 8%          → keep, do not accelerate
4. Surplus.             computed only after 1-3 are satisfied
```

`investing_allowed` is true only when **all** hold:

- no bleed (spend ≤ income)
- buffer ≥ 1 month of essentials
- no debt at or above the fire threshold

When `investing_allowed` is false, the investable surplus is zero, the book is
empty, and the Glider action is `none`. This is enforced structurally in
`allocation.py` and `glider.py` — it is not a recommendation the user can argue
with, because there is no code path that builds the other outcome.

### Debt thresholds

| Band | Default | Override |
|---|---|---|
| Fire | APR ≥ 15% | `MONEY_DEBT_FIRE_APR_PCT` |
| Judgment | 8% ≤ APR < 15% | Compare against the local risk-free rate from `reference.py` |
| Keep | APR < 8% | Do not accelerate; the money is worth more elsewhere |

The 8–15% band is a judgment call **on purpose**. In a high-rate currency the
local risk-free rate may itself be 18%, which makes 12% debt cheap. In a low-rate
currency 12% is expensive. That is exactly why the local risk-free rate is a
required input and not a decoration.

---

## 4. Decision tree (Stage 1 → Stage 6)

```
INTAKE
  └─ any of {income, essentials} missing?
       YES → problem_type = data_gap  → ASK, do not compute. STOP.

DIAGNOSE (first match wins)
  ├─ spend > income ............................. cashflow_bleed
  ├─ essentials known, buffer < 1 month ......... no_buffer
  ├─ any debt APR >= fire threshold ............. high_interest_debt
  ├─ cash-only savings in a currency where
  │  inflation > available deposit rate ......... inflation_erosion
  ├─ goal timeline < 3y but money in growth
  │  assets, OR goal > 7y held in cash .......... goal_mismatch
  ├─ costs already lean (< 60% fixed) and
  │  income is the binding constraint ........... under_earning
  ├─ buffer ok, no fire debt, nothing invested .. idle_surplus
  ├─ asks for leverage / memecoins / timing ..... overconfidence_risk
  └─ otherwise .................................. idle_surplus

DIAGNOSIS SENTENCE — one blunt line, before any plan. Emitted first.

SAFETY STACK
  └─ investing_allowed = false?  → book empty, surplus 0, glider none. STOP.

CASHFLOW SPLIT      → fixed / save / invest / guilt-free, in currency
ALLOCATION          → 70/30, or the override row that beat it, + reason
GLIDER MAPPING      → none | draft | monitor
PLAN                → 12-part MoneyPlan, disclaimer, what-would-change
```

Note the ordering: `high_interest_debt` outranks `inflation_erosion`. A user
losing 35% a year to a lender is losing faster than inflation is taking, and the
fire is the thing that can be put out this month.

---

## 5. Rejected advice

Each of these is struck out because it cannot survive real conditions. They are
recorded here so the rejection is a decision, not an oversight.

| Advice | Source | Why it is rejected |
|---|---|---|
| "Pay off the mortgage early, always" | Common US advice | In a high-inflation currency a fixed-rate loan is the cheapest money available. The general form is wrong; only the APR band decides. |
| "Invest 15% for retirement, always" | Ramsey | Meaningless without a currency, a wrapper and an inflation rate. 15% of a naira income in a 25%-inflation currency is not 15% of a dollar income. |
| "Max the 401(k) / Roth" | US-specific | No such wrapper exists for a Lagos cash earner. `R-AUDIT-4`. |
| "The market returns 8–10% a year" | Ramsey / folklore | A fabricated yield. We use no growth assumption we cannot source. |
| "Stocks always beat debt over 10 years" | Folklore | False at a 35% APR, which is where the Lagos fixture sits. |
| "Cut out the lattes" | Hustle canon | Ignores fixed-cost structure. A 60%+ fixed-cost ratio is not a latte problem. `R-SETHI-3`. |
| "Just save harder" | Generic | Spending is not a discipline problem, it is a design problem. `R-SETHI-5`. |
| "Put it all in crypto" | Retail internet | No diversification, and it is the definition of money you cannot afford to lose. `R-HOUSEL-2`. |
| "Timing the market beats time in it" | Retail internet | `overconfidence_risk`. `R-HOUSEL-3`. |
| Any employer-match-first rule | US/UK default | Only applies where a match exists; must be checked per user, never assumed. |
| "You got this" / motivational framing | — | Product rule: no sugar, no hustle porn. |
| Spreadsheet-based tracking | — | `R-SETHI-5`. |

---

## 6. What the rules cannot do

- Miriam is not a licensed adviser. Every plan carries a disclaimer.
- No rule here justifies inventing a yield, an APY, or a Glider performance
  figure. `R-AUDIT-2`.
- The 80/20 row requires an explicit, recorded acceptance of a 40% drawdown.
  Absent that acceptance, 70/30 is the ceiling.
