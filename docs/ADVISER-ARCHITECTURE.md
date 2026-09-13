# MIRIAM — Acting-Trusted Adviser Architecture

Status: Approved design (canonical reference for the build)
Owner: MIRIAM + RAIL_BACKEND
Related: `docs/python-agent-delegation-e2e.md` (RAIL_BACKEND), `docs/P1-BLUEPRINT.md`

> Goal product: a financial adviser that already knows your life and acts before
> you even ask — from "I moved £600 into your travel account" to "your salary
> came in, I cleared everything due and kept your savings goal on track."

This document is the source of truth for **how** we get there. It is split into
three parts: the north-star principles, the seven-layer system architecture, and
the two halves of the intelligence work that everything else depends on — the
**playbook/situation engine** and the **trust model**. The phased build plan and
v1 (P1) contracts live in `docs/P1-BLUEPRINT.md`.

---

## 0. North-star principles

1. **Go is the authority, Python is the intellect, the user is the final say.**
   Go owns money, identity, events, execution, and the approval ledger. Python
   owns reasoning, tone, and empathy. No amount of Python intelligence can
   bypass Go's execution boundary.
2. **Autonomy is earned, never implied.** Every action is classified by risk.
   First-time actions always confirm. Only explicit, auditable user mandates
   unlock repetition.
3. **Precision beats frequency.** Every proactive message must justify itself:
   *why now, why act, expected value, risk*. An annoyed user unsubscribes; a
   delighted user opens more doors.
4. **Everything is logged, measured, and fed back.** Every decision carries a
   receipt, every prediction tracks to an outcome, and the system tunes itself
   from acceptance/error rates.

---

## 1. Seven-layer system architecture

```
 ┌─────────── SIGNALS ───────────┐   Go event/outbox · calendar/email connectors
 │   money events · life signals │   · subscription watch … → normalized UserEvent
 └──────────────┬────────────────┘
 ┌────────── CONTEXT ───────────┐   Life+Money State (per-user, permissioned,
 │  balances · bills · goals ·  │   stale-guarded) · facts · memory · decisions
 │  subscriptions · calendar ·  └──────────────┬────────────────
 │  runway · forecast · past    │
 └──────────────┬────────────────┘
 ┌────────── TRIGGERS ──────────┐   event · cadence · forecast · behavior
 │        NOISE GATE ────┐      │   cheap deterministic first; LLM only on
 └──────────────┬────────┴──────┘   survivors
 ┌────────── REASONING ─────────┐   situation families → playbooks/primitive
 │ playbooks · forecast engine ·│   planning → decision module (LLM proposes,
 │ decision schema               │   spine disposes)
 └──────────────┬────────────────┘
 ┌────────── APPROVAL ──────────┐   tiers → mandate? → OTP? → caps/buffer/
 │  consent ladder · mandates · │   kill-switch → audit + receipts
 └──────────────┬────────────────┘
 ┌────────── EXECUTION ─────────┐   Go action executors (idempotent, existing
 │  existing Go REST money path │   REST, ledger-backed, revert-capable)
 └──────────────┬────────────────┘
 ┌── DELIVERY + FEEDBACK ───────┐   bridge (iMessage) · cards · quiet hours ·
 │  message throttles · outcomes│   outcome loop → back into TRIGGERS
 └──────────────────────────────┘
```

### Layer 1 — Signals (the input: money events + life signals)

Push beats poll. Go already emits a rich event surface (ledger outbox style).
We define a **proactive-relevant subset** of money events:

`deposit.salary`, `deposit.refund`, `withdrawal.large`, `bill.due_soon`,
`bill.paid`, `subscription.renewing`, `subscription.charged`,
`fx_charge`, `low_balance`, `goal.regression`, `goal.milestone`.

Life signals (P3) come from permissioned connectors — Google Calendar and
Gmail (travel, moves), subscription stores (Go `subscriptions` table) — and are
materialized into the **same event shape**.

**`UserEvent`** — single normalized schema (idempotent):

```
{ user_id, type, occurred_at, payload, source, fingerprint }
```

`fingerprint` is a stable hash so the same deposit replayed through the outbox
can never double-trigger.

### Layer 2 — Context: the Life+Money State

A per-user object the reasoner reads, rebuilt fresh on demand by Go (which has
all the data as authority):

```
life_money_state = {
  balances, upcoming_bills[], subscriptions[].renews_at,
  calendar_events[] (permissioned), goals[], savings_target,
  runway_days, forecast {shortfall, confidence}, recent_events[],
  past_decisions[] (already told?), facts[], staleness_ts
}
```

- Extension of the current `financial-snapshot` endpoint (adds bills,
  subscriptions, calendar slice, facts, decisions).
- **Staleness guard:** every decision must witness a `staleness_ts`; stale
  context → refetch, never guess.
- Python stays **stateless**: same state in → same decision out → reproducible,
  testable.

### Layer 3 — Triggers + the noise gate

Four trigger types raise `{user_id, trigger}`:

| Trigger | Source | Example |
| --- | --- | --- |
| Event | Layer 1 | salary landed, bill due tomorrow |
| Cadence | scheduler | daily review, weekly recap |
| Forecast | re-run when data moves | shortfall > threshold, runway < X |
| Behavior | pattern detector | subscription unused 60d, overspend drift |

Every trigger passes a **noise gate — cheap, deterministic, stateful — before
any LLM spend**:

- dedupe window (already-told? same topic?)
- message caps (per-user daily, per-topic cooldown)
- quiet hours (per-user timezone)
- value floor (bill ≤ £2? ignore)
- explicit ignore rules (user said "stop telling me about X")

Only survivors reach reasoning. This wall is what makes the product present
rather than nagging.

### Layer 4 — Reasoning (playbooks + decision module)

See Part II below. Output is always a structured decision, never free prose and
never a raw intent to touch money.

### Layer 5 — Approval

See Part III (trust model) below. Enforced in Go at the execution boundary.

### Layer 6 — Execution

Reuses Go's existing REST money path (stash/wallets, `/bills/pay`, cards,
subscriptions) with existing `idempotency_keys`. Python never calls these APIs;
Python produces decisions, Go executes after the approval layer clears them.

### Layer 7 — Delivery + feedback

- Proactive messages go through the existing bridge (iMessage) via the reacher
  worker, extended to **event-driven delivery** + a priority queue + quiet hours
  + per-user caps.
- **Feedback loop** (tables already scaffolded): predictions → outcomes, weekly
  self-review, tone profiles. Every proactive action tracks an outcome —
  `accepted / ignored / declined / reverted / saved_£` — and weekly reviews
  tune trigger weights and playbook thresholds.

---

## II. The situation engine (playbooks as data, not features)

Treating playbooks as a hardcoded feature list is the wrong model. Five playbooks
become twenty, then a hundred, and we are forever chasing scenarios. Instead
playbooks are **declarative compositions over a small, governable set of
primitives**.

### Layer A — Action primitives (the true power boundary)

The unit of control. Playbooks never invent capabilities; they compose these.
Each primitive carries its **trust profile** (tier, reversibility, revert
window, caps, mandate-eligibility) — that profile is what the trust model reads.

| Primitive | Go endpoint | Tier | Reversible | Note |
| --- | --- | --- | --- | --- |
| `send_nudge` / message | bridge | T0 | — | throttled |
| `adjust_budget(plan)` | budgets | T1 | yes (24h) | |
| `set_savings_goal_funding` | goals | T1 | yes (24h) | |
| `enable_roundups` | settings | T1 | yes (24h) | |
| `cancel_subscription` | subscriptions | T1 | yes (30d) | renew-window actions |
| `move_funds(from,to,amount)` | stash/wallets | T2 | yes (24h) | OTP unless mandate |
| `pay_bill(bill_id)` | /bills/pay | T2 | — | OTP unless mandate |
| `set_card_fx_mode` | cards | T1/T2 | yes | trip-scoped |
| `freeze_card` | cards | T1 | yes | |
| `report_dispute` | cards | T3 | — | always confirm |

Bounded primitives = provable safety. The whole advert ("move £600", "cancel
Spotify", "disable FX fees") is already on this list.

### Layer B — Situation families, not playbook names

Every scenario compresses into a handful of families that generalize infinitely:

| Family | Trigger shape | What it does |
| --- | --- | --- |
| `INFLOW` | deposit / refund / salary / windfall | distribute: dues → goals → buffer → spendable (the "left you £740 to spend" pattern) |
| `OUTFLOW` | bill due / renewal / subscription / big charge | cover, trim, or triage |
| `GOAL` | savings target / travel / debt / house-move | progress, funding, adjustment |
| `RISK_EXPOSURE` | FX fees / overspend / fees / fraud signal | mitigate + message |
| `LIFE_CHANGE` | travel / move / new job / baby / marriage | re-plan the whole money posture |

"Your flight to Brazil is booked" = `LIFE_CHANGE`(travel) composed with
`GOAL`/`INFLOW`. One scenario now, infinite later.

### Layer C — Playbook anatomy (declarative, registry-backed)

A playbook is a config object in `playbook_registry`:

```yaml
id: salary_distribution
family: INFLOW
when:                        # trigger spec: event | cadence | forecast | user-declared
  events: [deposit.source == salary]
context_requires: [balances, bills_due, goals]
tier_policy:                 # hooks into the trust model
  default_tier: T2
  mandate_eligible: true
  cap_pct_available: 0.5
analyze:                     # rules + LLM fill this schema (see decision schema)
  intent: "..."
  options[]: {action, amount, tier, leverage_£, reversibility, reason}
comms: {message_tmpl, card, quick_replies, message_vs_wait_rule}
outcomes: [accepted, ignored, declined, reverted, saved_£]
```

Adding "rent rises in 3 months → pre-budget" = a `yaml` PR, not a feature arc.
New code is only needed for a **new primitive** (rare) or a **novel analyzer**
(also rare).

### Layer D — Open-ended reasoning

- **The user is a trigger source.** "I'm moving in February" or "cancel stuff I
  haven't used in 2 months" → NLP → parsed into a **sentinel** + playbook +
  mandate scoped to their words. Highest-trust input there is: derived from the
  user's own intent, not our guesses.
- **Sentinels** = long-horizon watches ("watch my subscriptions", "track the
  rent raise"). Per-user scheduler entries that re-evaluate on cadence/events.
  This is how "acts before you ask" sustains across weeks, not one-shot.
- **LLM composition for the tail**: where no playbook matches, the LLM proposes
  a plan *from primitives only*. The spine (trust model + caps) still disposes.
  Creative skin, deterministic spine.
- **Staleness guard**: a decision on stale balances is a trust violation.

---

## III. The trust model (deep design)

Trust is the product. It is not a permission check; it is a **stateful,
learning, auditable economic system** around the user's money.

### 3.1 The consent ladder (how much authority, granularly)

```
LEVEL 0  Baseline membership     read-only + messages + analytics
LEVEL 1  One-off explicit        every action confirmed (today's OTP)
LEVEL 2  Behavioral standing     "yes, and do it automatically" → mandate
LEVEL 3  Declared rules          the user's own words become auto-rules
LEVEL 4  Fiduciary-style         "manage everything, keep £X buffer" — hard-capped
```

- Consent is **per action-class, per subject, per amount** — never global.
- The ladder is a **vector, not a rank**: a user at Level 2 for "subscriptions"
  stays Level 1 for "large transfers."
- Advancement is by correctness; regression by outcome (3.5).

### 3.2 The decision matrix (what needs what)

Every candidate action is classified at decision time on four axes:

| Axis | Values |
| --- | --- |
| Reversibility | reversible (24–48h undo) / irreversible (paid, cancelled) |
| Magnitude | absolute cap · daily cumulative · % of available liquid |
| Novelty | first-ever / known pattern / stood-up mandate |
| Disruption | fees, dishonor, embarrassment on a wrong move? |

Resulting tiers (enforced in Go at execution boundary, never trusted to model):

| Tier | Character | Rule |
| --- | --- | --- |
| T0 | read / message / nudge | free, throttled |
| T1 | reversible, small | mandate → auto; else OTP |
| T2 | moves real money | **always OTP** unless mandate + cap match |
| T3 | irreversible, large, novel | OTP + double-check; **never** pure-auto |

### 3.3 Mandate engine (standing consent, fully audited)

Fleshes out the existing `miriam_mandates` table:

```
{ id, user_id, action_class, condition(json), max_amount,
  daily_cap, monthly_cap, priority, cap_pct_available,
  valid_from/until, next_run, enabled,
  source: otp_confirm | declared_rule | behavioral,
  created_at, revoked_at }
```

Auto-execution **only** when all hold:
1. mandate matches the candidate action
2. amounts within per-action + daily + monthly caps and `cap_pct_available`
3. user not paused/frozen, not quiet-hours-blocked
4. dedupe fingerprint + cooldown pass (no double-fire)
5. buffer floor still intact after execution

Every auto-execution emits: receipt, notification, grace-revert window
(reversible classes), and a "what I did & why" message. A mandate never
silently widens scope.

### 3.4 Hard safety rails (stacked, in Go — not the model)

1. **Caps everywhere**: per-action, per-day, per-month, % of liquid, absolute £.
   Enforced at the execution boundary. A buggy/hallucinating model physically
   cannot exceed them.
2. **Buffer protection by default** — the "left you £740" rule: never move below
   a configured floor (fixed £X or last-30-day average). Non-overridable by the
   model; only the user lowers it.
3. **Kill-switches**: "pause all autonomy" / "messages only" / "full freeze",
   plus per-class toggles; `STOP` via iMessage. Instant, aborts in-flight before
   execute.
4. **Idempotency + fingerprints**: events, decisions, actions all deduped.
   Double events ≠ double payments.
5. **Escalation, never silent-skip**: over-cap or out-of-mandate → downgrade to
   OTP confirm with a reason. Never quietly drop.
6. Reuse existing Go controls: `transaction_limits`, `money_guard`, blocks,
   quiet hours + the new sentinel scheduler.

### 3.5 Trust as a dynamic state (learned, per user)

A trust vector from history: acceptance rate, ignore rate, decline rate, revert
rate, **error rate** (things the user had to undo). It gates the consent ladder:
new users are confirm-only; consistent correctness unlocks mandates and higher
caps; reverts/errors actively shrink it. Internal risk control — never gamified
at the user — but "why I asked" is always surfaced. Transparency *is* trust.

### 3.6 The trust ledger (auditability = confidence)

- **decision_receipt**: trigger, playbook, context_hash+ts, options considered,
  chosen option, confidence, tier, mandate_id, reason, *refusals* (why not the
  others — powers "why didn't you cancel X?").
- **action_receipt**: action, amount, idempotency, pre/post balance snapshot,
  success/failure.
- **outcome**: accepted / ignored / declined / reverted / saved £.
- **weekly_self_review** (tables exist): precision, money saved, false positives
  → auto-tune thresholds; sends an honest summary to the user.

The ledger is *the* answer to "why did you do that and was it worth it." If we
can't answer that for every action, we do not ship autonomy.

### 3.7 Model-risk control (the wild-LLM threat)

- The LLM **never calls money APIs**. It emits a constrained JSON decision
  validated twice — Pydantic on Python, then Go at the execution boundary.
- **Decision output contract** (P1 already implements a first form):

```json
{
  "intent": "describe what she wants to happen",
  "action_class": "must be in primitives registry",
  "amount": "0 < amount <= cap",
  "tier": "T0..T3",
  "reasoning": "why now, why act, expected value",
  "cites": ["ids of snapshot rows used"]
}
```

- Amount out of bounds → reject. Unknown action class → **error + operator
  alert**; never silent-skip, never execute.
- Constrained tool schemas + sanity bounds + reversibility validated against the
  primitive registry.
- **Every prediction is a promise**: "you'll be £1,800 short" tracks
  prediction → outcome; calibration feeds message language ("high confidence"
  only when actually calibrated).

---

## IV. How the two halves interlock

- Playbooks **declare** their tier/caps/mandate-eligibility; the trust model
  **decides** whether that playbook is permitted at that tier *for this user, at
  this moment*.
- New primitives (rare) extend the registry; new playbooks (common) become
  config + a one-line trust-profile review.
- The trust ledger is the shared source of truth both read: playbooks for
  feedback (measure `saved £`), the trust model for gating.

---

## V. Phase map

| Phase | Outcome | Key work |
| --- | --- | --- |
| **P1** | Act-before-ask MVP: event-driven proactivity, T0/T1 messaging autonomy, T2 via OTP, receipts | event→decision spine, `intel/evaluate` API, primitives registry, noise gate, decision receipts + outcome tracking |
| **P2** | Mandates + sentinels: auto-execution for approved rules, user-declared rules, revert windows, dynamic trust state | mandate service, approval state machine, sentinel scheduler |
| **P3** | Life signals: calendar + email connectors; travel/subscription/FX playbooks | connectors, `UserEvent` schema, permissioned consent |
| **P4** | Level 4 fiduciary posture + learning loop | prediction calibration, weekly self-review tuning, expanded registry |

**P1 is the spine** — everything after rides on it. Build the primitives
registry + trust profile + decision schema carefully now; the rest is
composition.

---

## VI. Open decisions (resolve before/in P1)

1. **Quiet hours timezone**: per-user TZ from profile or device? Default UTC until
   consent is collected.
2. **Sentinel scheduler home**: a Go task in the existing worker (cron) — confirmed
   default; Python stays stateless.
3. **Calendar/email consent storage**: store OAuth tokens in Go's vault-secret
   pattern; never in Python.
4. **Buffer floor default**: `max(£0, last_30d_avg_spend * 1.1)` as default floor;
   user-overridable, model non-overridable.
5. **Revert window width**: 24h for in-app reversible, 48h for subscription
   cancellations within renewal windows.