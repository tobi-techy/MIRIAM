# MIRIAM Onboarding - Actual Implementation Architecture Map

## State Machine (from `miriam_agent/onboarding/state.py` and `miriam_agent/onboarding/service.py`)

### Stages (in order)
1. **STAGE_GREETING** = "greeting" - First message, learn user's name
2. **STAGE_INTERVIEW** = "interview" - Main conversation, extract facts
3. **STAGE_AWAITING_STATEMENT** = "awaiting_statement" - Request bank statement (optional)
4. **STAGE_PLAN_CONSENT** = "plan_consent" - Present plan, ask for consent
5. **STAGE_AWAITING_ADJUSTMENT** = "awaiting_adjustment" - User wants to change plan
6. **STAGE_COMPLETE** = "complete" - Onboarding finished

### State Transitions (from `_TRANSITIONS` in service.py)

| Current Stage | Intent | Next Stage | Action |
|---|---|---|---|
| INTERVIEW | interview | INTERVIEW | stay |
| INTERVIEW | request_statement | AWAITING_STATEMENT | request_statement |
| INTERVIEW | present_plan | PLAN_CONSENT | present_plan |
| INTERVIEW | abandon | COMPLETE | abandon |
| AWAITING_STATEMENT | request_statement | AWAITING_STATEMENT | request_statement |
| AWAITING_STATEMENT | present_plan | PLAN_CONSENT | present_plan |
| AWAITING_STATEMENT | abandon | COMPLETE | abandon |
| PLAN_CONSENT | consent_yes | COMPLETE | complete_automated |
| PLAN_CONSENT | consent_no | COMPLETE | complete_draft |
| PLAN_CONSENT | adjust | AWAITING_ADJUSTMENT | adjust |
| PLAN_CONSENT | abandon | COMPLETE | abandon |
| PLAN_CONSENT | interview | PLAN_CONSENT | stay |
| AWAITING_ADJUSTMENT | adjust | AWAITING_ADJUSTMENT | adjust |
| AWAITING_ADJUSTMENT | done_adjusting | PLAN_CONSENT | present_plan |
| AWAITING_ADJUSTMENT | abandon | COMPLETE | abandon |
| AWAITING_ADJUSTMENT | interview | AWAITING_ADJUSTMENT | stay |

### Stage-Intent Whitelist (from driver.py)
- INTERVIEW: {"interview", "request_statement", "present_plan", "abandon"}
- AWAITING_STATEMENT: {"request_statement", "present_plan", "abandon"}
- PLAN_CONSENT: {"consent_yes", "consent_no", "adjust", "abandon", "interview"}
- AWAITING_ADJUSTMENT: {"adjust", "done_adjusting", "abandon", "interview"}

## Key Components

### 1. OnboardingState (state.py)
Persisted per-user in Redis (with in-process fallback):
- `stage` - current stage
- `name` - user's first name
- `money_moment` - opening "what's bothering you about money"
- `money_moment_meta` - structured: emotion, suspected_problem, confidence
- `goal` - concrete goal ("Japan trip 2027")
- `goal_meta` - structured: target_date, estimated_cost, priority, funding_status
- `learned` - free-form facts dict (agent's own labels)
- `document_summary` - bank statement scan from Go backend
- `plan` - deterministic plan from plan.py
- `plan_presented` - boolean
- `adjustments` - list of user adjustment notes
- `interview_turns` - counter for cap
- `conversation_state` - deterministic read (topic, problem, sentiment, etc.)
- `started_at`, `completed_at`, `updated_at` - timestamps

### 2. Conductor (driver.py)
LLM-led conversation using `CONDUCTOR_SYSTEM_PROMPT`:
- Returns structured `DriverOutcome`: reply, suggested_replies, facts, intent, adjustment, reaction
- Uses `emit_conductor_outcome` tool (with JSON fallback)
- Strict schema validation via `ConductorOutcome` (pydantic, extra=forbid)
- Sanitizers bound reply length, taps, facts

### 3. Plan Builder (plan.py)
Deterministic, pure functions - NO LLM:
- Sniffs text for signals (regex patterns)
- Diagnostic states: Stability Seeker, Volatile Earner, Wealth Builder, Financial Beginner
- Overlays: debt_burden, family_obligations, spending_leakage, goal_urgency
- Steps: buffer, income_rhythm, obligations, debt, spending_guard, goal, checkin
- Standing rules (only for hands-on users)
- Financial insight (spec §21) - category, title, summary, severity, confidence, recommended_action
- Adjustments folded in deterministically

### 4. Financial Profile (financial/profile.py)
Structured profile with provenance:
- `FinancialFact` - value + source + confidence + currency + note + timestamp
- Merge policy: user_correction > user > statement/ledger > conversation > inferred
- `extract_money_facts()` - deterministic NL extraction of amounts, frequencies, currencies
- `profile_from_onboarding_state()` - bridges conversational state to structured profile

### 5. Service Orchestration (service.py)
`OnboardingService.handle_turn()` - main entry point:
- Loads state from store
- Handles document (bank statement) upload
- Greeting → name capture → interview
- Interview cap (ONBOARDING_MAX_QUESTIONS)
- Money action detection (hands off to general agent)
- Poll vote handling (deterministic for statement/consent)
- Conductor turn → outcome → transition → dispatch
- Plan presentation → consent → completion
- Memory persistence via `_remember()`
- Trace emission for observability

### 6. State Store (state.py)
`OnboardingStateStore` - Redis with local fallback:
- Sliding TTL (ONBOARDING_STATE_TTL_DAYS, default 30)
- Schema migration (v1 → v2)
- Fail-open: Redis failure never breaks conversation

## Data Flow

```
User Message
    ↓
OnboardingService.handle_turn()
    ↓
[Greeting] → capture name → STATE_INTERVIEW
    ↓
[Interview] → conductor_turn() → LLM extracts facts + intent
    ↓
State machine validates intent → transition
    ↓
[If present_plan/request_statement] → build_plan() (deterministic)
    ↓
[Plan Consent] → present_plan_turn() → LLM presents in voice
    ↓
[Consent] → yes → standing rules persisted to memory
            no → draft saved
            adjust → AWAITING_ADJUSTMENT → rework → re-present
    ↓
[Complete] → onboarding done, general agent takes over
```

## Key Safety Features

1. **Stage-intent whitelist** - model intent filtered by current stage
2. **Deterministic plan builder** - LLM never calculates money
3. **Structured output validation** - pydantic contracts at every boundary
4. **Fail-open LLM** - any error falls back to short human reply
5. **Money action detection** - `_wants_money_action()` bypasses onboarding
6. **Idempotency keys** - deterministic per (user, tool, args)
7. **Merge policy** - user corrections always win over inferences
8. **Currency assumptions recorded** - never silent defaults
9. **Audit trail** - every tool call logged with amount/recipient
10. **Safety policy** - daily/tx limits, KYC checks, suspicious patterns

## External Integrations

- **Go Backend** - balances, transactions, wallets, KYC, investments, billpay
- **Redis** - onboarding state persistence (sliding TTL)
- **PostgreSQL** - conversations, messages, memory entries, financial profiles
- **Supermemory** - long-term memory graph (fail-open)
- **LLM Provider** - OpenAI/Concentrate for conductor and plan presentation

## Configuration (settings.py)
- `ONBOARDING_ENABLED` - feature flag
- `ONBOARDING_MAX_QUESTIONS` - interview cap (default 12)
- `ONBOARDING_STATE_TTL_DAYS` - Redis TTL (default 30)
- `ONBOARDING_TEMPERATURE` - LLM temperature
- `ONBOARDING_MAX_TOKENS` - LLM max tokens
- `MAX_DAILY_TRANSFER` / `MAX_TRANSACTION_AMOUNT` - safety limits