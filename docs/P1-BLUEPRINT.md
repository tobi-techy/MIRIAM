# P1 Blueprint — Act-Before-Ask MVP

Reference: `docs/ADVISER-ARCHITECTURE.md` (canonical design).
Outcome of P1: **event-driven proactivity is live and safe.**
- T0/T1 proactive *messages* fire autonomously (salary, bill-due, renewal watch).
- T2 money moves always go through OTP confirmation (today's flow) — mandates are
  P2, so P1 makes NO autonomous money moves.
- Every decision + delivery is recorded (receipts), every outcome is tracked.

---

## 1. Scope (P1 does / does not)

| Does | Does not |
| --- | --- |
| Event-driven triggers for salary / bill-due / subscription-renewal / low-balance | Autonomous T2+ money moves (no mandates in P1) |
| `intel/evaluate` decision API + primitives registry | Life-signal connectors (calendar/email) — P3 |
| Deterministic noise gate before any LLM call | Dynamic per-user trust state — P2 |
| Decision receipts + outcome tracking | User-declared rules / sentinels — P2 |
| Delivery via Go reacher (receives decisions, throttles, sends) | |

Safety stance for P1: **messages-only autonomy + OTP for money.** This is strict
by design; it gives the ledger and the metrics a clean baseline before any
standing consent is introduced in P2.

---

## 2. New/changed components

```
Go (scheduler + execution + ledger)           Python (intelligence, stateless)
──────────────────────────────                ──────────────────────────────
outbox/event watcher   ──POST /intel/evaluate──▶ evaluate_trigger()
  EventRouter (watchers ╲                       │  trigger_classifier (rule-first)
  cap/quiet-hours/      │   {user_id, trigger,  │  playbook_match → families
  dedupe pass)          │    context_fetched}   │  decision module (LLM, schema
receipt write ◀────────┘                        │  validated twice; Pydantic)
  decision_state                                  decision draft returned
                                                    │  noise gate (deterministic,
                                                    │  cheap, Redis state)
                                                    ▼
                                             decision (approved/quiet) 
```

### Go side
- **Event watcher** over the existing outbox/event surface, filtered to the P1
  event set (`deposit.salary`, `bill.due_soon`, `subscription.renewing`,
  `low_balance`, `deposit.refund`, `withdrawal.large`).
- **Pre-gate** before the API call: per-user daily message cap, quiet hours,
  24h dedupe (fingerprint), topic cooldown, feature/per-user toggle. Same class
  of guard as the Python store but authoritative (Go decides to *call or not*).
- **Reacher worker** extended: on a decided `message`, apply delivery policy
  (priority queue, quiet hours, iMessage thread via bridge) and persist the
  receipt on the `miriam_decision_receipts` table.
- **Receipt writer**: every call to Python records `{user_id, trigger, context_hash,
  decision, message, amount(0), tier, created_at}`.

### Python side
- **`miriam_agent/intel/`** new package (keep separate from `proactive/` which
  stays the time-based analyst):
  - `primitives.py` — registry: each primitive = `(name, action_class, tier,
    reversibility, revert_window, mandate_eligible, default_cap)`. In P1 this is
    the *declaration source*; Go mirrors/validates it.
  - `triggers.py` — event→family matcher (rule-first: mapping event types to
    families, cheap).
  - `decision.py` — the decision schema (Pydantic), `DecisionDraft`, and the
    `.validate()` that the LLM output must pass (amount in `(0..cap]`,
    `action_class ∈ primitives`, `tier` coherent).
  - `playbook_registry.py` — declarative playbooks (yaml) for the P1 set:
    `salary_distribution (INFLOW)`, `bill_triage (OUTFLOW)`,
    `subscription_renewal (OUTFLOW)`, `buffer_watch (OUTFLOW/RISK)`,
    `low_balance (RISK_EXPOSURE)`.
  - `noise.py` — deterministic noise gate (Python half): dedupe, caps,
    cooldown; reads the same Redis-backed `ProactiveStateStore`.
  - `api/intel.py` — FastAPI router `POST /api/v1/intel/evaluate`.
- **Decision output contract** (model risk): reused by `intel` and gradually by
  the analyst. See ADVISER-ARCHITECTURE 3.7. The LLM proposes inside the schema;
  Go disposes at the boundary.

---

## 3. API contract — `POST /api/v1/intel/evaluate`

Request (minted by Go; `Authorization: Bearer <agent_jwt>`, 120s TTL as today):

```json
{
  "user_id": "u_123",
  "trigger": {
    "type": "event",
    "kind": "deposit.salary",
    "occurred_at": "2026-09-13T09:00:00Z",
    "fingerprint": "sha256:...",
    "payload": {"amount": 3400000.0, "source": "salary_ngn"}
  },
  "context": {
    "fetched_at": "2026-09-13T09:00:10Z",
    "balances": ["..."],
    "upcoming_bills": ["..."],
    "subscriptions": ["..."],
    "goals": ["..."],
    "recent_events": ["..."],
    "facts": ["..."],
    "past_decisions": ["..."]   // already-sent receipts (dedupe)
  }
}
```

Response:

```json
{
  "decision": "message" | "quiet",
  "family": "INFLOW",
  "playbook_id": "salary_distribution",
  "message": "£34,000 landed. 3 bills due before payday — want me to clear them and
              keep your goal on track?",
  "options": [
    {"action_class": "pay_bill", "target": "bill_1", "amount": 12000.0,
     "tier": "T2", "leverage_ngn": 0, "reversibility": "irreversible",
     "reason": "due 26 Sep; covered by deposit"}
  ],
  "confidence": 0.82,
  "cites": ["bal:op1", "bill:1023"],
  "reasoning": "Salary inflow clears all short-dated bills while preserving goal buffer."
}
```

`decision: "quiet"` for noise-gated/uncertain cases (with a `reason`). P1 rule:
`quiet` unless the LLM is confident and the message passes the noise gate.

---

## 4. Noise gate (deterministic, cheap, before LLM)

Chain (fail-closed at the call boundary — Go decides whether to even send):
1. fingerprint already seen? → quiet
2. same topic messaged < 24h? → quiet
3. per-user daily message cap (`MAX_PROACTIVE_MESSAGES_DAY`, default 3)? → quiet
4. quiet hours (per-user TZ)? → queue for later
5. value floor (e.g., bill ≤ £2, deposit ≤ £0)? → quiet
6. user-level feature toggle / "messages only" / freeze? → quiet/refuse

Passes 1–6 → build context → LLM reasoning. LLM output revalidated (schema +
amount bounds) → decision. This is the anti-annoyance wall.

---

## 5. Settings additions (Python)

```yaml
INTEL_ENABLED: bool = True
INTEL_EVENT_SET: list[str] = ["deposit.salary","deposit.refund","withdrawal.large",
                              "bill.due_soon","subscription.renewing","low_balance"]
MAX_PROACTIVE_MESSAGES_DAY: int = 3
MIN_BILL_VALUE_TO_REACH_OUT: float = 200.0   # NGN
BUFFER_FLOOR_RATIO: float = 1.0              # x last-30d avg spend (P1: informational)
```

---

## 6. Data / schema deltas

- Go `miriam_decision_receipts` (add `tier`, `family`, `playbook_id`, `options`
  json, `trigger_fingerprint`, `decision`).
- Python: no new persisted tables for P1 beyond existing `audit_logs` /
  `tool_usage` / `proactive_state` (Redis) — decisions are authoritative in Go.
  (Add `intel_decisions` later if offline debugging warrants it.)

---

## 7. Test plan (P1)

- **Unit**: primitive registry completeness/tier coherence; trigger→family matcher;
  schema validator (amount over cap → reject); noise gate chains (dedupe, caps,
  quiet hours).
- **Integration (Python)**: `intel/evaluate` with a canned Go-shaped context →
  deterministic message/quiet decisions for each P1 playbook without calling the
  model (inject a stub provider).
- **Integration (Go)**: outbox event → pre-gate → calls Python → receipt written;
  delivery respects caps and quiet hours.
- **E2E**: real event (salary) → message lands in iMessage thread; a T2 proposal
  requires OTP; receipts visible; no double-fire on replay.

---

## 8. Milestones

1. `intel` package + primitives registry + decision schema (+tests) — Python only.
2. Noise gate + `intel/evaluate` endpoint (+stubbed tests).
3. Go: event watcher + pre-gate + reacher delivery + receipt write.
4. E2E salary & renewal scenarios on staging; wire into prod behind
   `INTEL_ENABLED`.
5. Metrics review: outreach precision after 2 weeks → tune thresholds (P2 input).

## 9. Out of scope gates (check before proceeding)

- Any autonomous money move requires the P2 mandate engine + revert paths +
  dynamic trust state to exist. P1 ships strict.
- Prediction calibration is tracked but not enforced until P4.