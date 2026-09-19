# Miriam Production-Readiness Audit

**Date:** 2026-09-17 · **Scope:** `miriam_agent/` Python intelligence layer (Miriam) · **Branch:** `main`
**Authority:** This is a static, read-only audit. No code was modified. Findings cite `file:line`.

---

## 1. Executive Summary & Verdict

Miriam is a well-structured, unusually disciplined Python agent: full tool metadata (risk /
mutation / approval flags) drives RBAC, system prompts, and the staging loop from one registry; money is
delegated to the Go backend (Rail) as the ledger authority; trace ids thread through requests,
tool records, audit rows, and OTEL spans; and there is a written architecture contract (`docs/ARCHITECTURE-CONTRACT.md`)
plus an active regression culture (399 tests, several written specifically to close earlier "silently dead"
safety checks).

**The architecture can be safely developed further, with three carve-outs that must be fixed before
real-money production and ideally before the next milestone:**

1. **The money-approval invariant is enforced by the client round-trip, not by the server.** Python
   executes whatever `approved_actions[]` the client puts in the request body (`agent_loop.py:142-168`),
   with no server-side record that a matching proposal was ever staged, no nonce, no pinning. The docstring
   invariant ("confirmation attached to the exact proposed action", `agent_loop.py:13-14`) is a signature
   match against *client-supplied* data. The effective firewall is JWT possession + Go-side checks.
2. **JWT trust assumes Go tokens, but Python shares the signing secret and has minting code.** Default
   `JWT_SECRET="change-me-in-production"` (`settings.py:59`); the compose file ships without setting it;
   `create_token()` exists (`jwt.py:36-52`, currently no callers); `get_current_user` derives roles
   straight from claims and promotes `verified: true` to the `execute` role (`dependencies.py:56-80`).
   If the default secret ships even once, anyone can forge a verified user.
3. **A boolean comparison bug makes `SafetyPolicy`'s approval thresholds non-functional** (`policy.py:86`,
   `policy.py:614`), and the streaming endpoint evaluates every money action as a **guest** because it passes
   `user_context=None` into `_safe_execute` (`agent_loop.py:357-358,455-457,490-491`) — money over
   `/chat/stream` silently fails RBAC.

**Verdict:** Continue building the product on this foundation — the layering, tool metadata model, and
delegation-to-Go are the right shape. But treat the confirmation-model and JWT-secret findings as P1
release blockers, and the streaming-RBAC regression as P1 for any flow that confirms money over streaming.
Everything else in this report is P2/P3 (hardening).

---

## 2. Scope, Method & Evidence

**Method:** Static read-through (Read/Glob/Grep), plus `pyproject.toml`, Docker/compose, `.env.example`,
OTEL config, docs, and the full test inventory. No runtime execution, no `.env` (secrets), no network calls.
Uncommitted WIP in `miriam_agent/documents/` and modified `config/settings.py` / `pyproject.toml` /
`uv.lock` was noted but **not treated as audited architecture**.

**Primary evidence files (read in full):**
`agents/agent_loop.py`, `agents/tools.py`, `auth/rbac.py`, `auth/jwt.py`, `api/dependencies.py`,
`api/chat.py`, `api/main.py`, `api/proactive.py`, `config/settings.py`, `database/models.py`,
`safety/policy.py`, `safety/validator.py`, `safety/audit.py`, `core/security.py`,
`database/memory.py`, `database/connection.py`, `database/working_memory.py`,
`integrations/go_client.py`, `integrations/supermemory_client.py`,
`conversational/supermemory_memory.py`, `tools/definitions.py`, `tools/investment_definitions.py`,
`agents/concentrate.py`, `agents/llm.py`, `observability/*`, `onboarding/service.py`,
`financial/intelligence.py`, `Dockerfile`, `entrypoint.sh`, `docker-compose.yml`, `docker-compose.dev.yml`,
`.env.example`, `otel-config.yaml`, `docs/ARCHITECTURE-CONTRACT.md`, `docs/P1-BLUEPRINT.md`,
`tests/test_safety_policy.py`, `tests/` inventory (399 tests / 25 files).

**Classification:** P0 = ship-blocking / arbitrary-money risk; P1 = release-blocker for real money;
P2 = should fix soon; P3 = hardening. Items marked **UNKNOWN** mean the Go backend (not present in this
repo) is the deciding factor and could not be verified.

---

## 3. System Map

```
 Clients (web / iMessage·WhatsApp bridge)
        │  Bearer JWT (Go-issued, HS256, shared secret)
        ▼
 ┌───────────────────────────── miriam-agent (Python, FastAPI) ─────────────────────────────┐
 │ api/main.py        CORS, trace middleware, /health /health/ready /metrics                   │
 │ api/chat.py        POST /api/v1/chat, /chat/stream, /conversations*                        │
 │ api/proactive.py   POST /api/v1/proactive/analyze  (called by Go reacher)                  │
 │ onboarding/service.py  LLM-led interview state machine (polls → plan → consent)            │
 │ agents/agent_loop.py   orchestrator: prompt build → LLM → tool split (auto vs staged)      │
 │ agents/tools.py        ToolRegistry: metadata, JSON-schema validation, observers, telemetry │
 │ agents/llm.py, concentrate.py  Concentrate primary / OpenAI failover                       │
 │ safety/policy.py, validator.py, audit.py   policy, rate limits, audit trail                │
 │ auth/rbac.py, jwt.py        role gates, token decode (role from claims)                    │
 │ tools/*.py         ~42 typed adapters → Go REST (money) / Supermemory / local compute       │
 │ financial/, investments/, proactive/   deterministic math + Glider investment adapters     │
 │ database/, vector/, conversational/     Postgres (memory, audit), Redis, pgvector, SM      │
 │ observability/     structlog, Prometheus, OTel, correlation/trace                          │
 └──────────────────────────────────────────┬────────────────────────────────────────────────┘
                                            ▼
                    rail-backend (Go)  ::  authoritative: identity, ledger, money movement,
                        JWT issuance, OTP confirmation, quiet-hours/caps for proactive,
                        investment previews + confirmation tokens (Glider), health
```

**Trust boundaries (per architecture contract):** Go is authoritative for identity/financial state/money.
Python is "intelligence, stateless": it may *read* financial state and *propose/wrap* money actions, but
every mutation is delegated to Go with the user's JWT. Miriam never writes ledger state.

**Actual deviations found (this audit):**
- Python can mint JWTs with the same secret (`auth/jwt.py:36-52`) — capability is dormant (no callers),
  but it violates "Go is the identity authority".
- Python writes records Go does not own (local users, memory_entries, audit_logs, tool_usage,
  financial_profiles, conversations/messages) — allowed but means the local DB holds PII with its own lifecycle.
- Python has read-write access patterns to its own DB via `financial/`, `database/`, `onboarding/`
  (see §25 for contract-layering status).

---

## 4. Repository & Packaging

`pyproject.toml` (PEP 621, hatchling, uv-managed): Python ≥ 3.11; runtime deps FastAPI, uvicorn,
pydantic v2 + pydantic-settings, SQLAlchemy 2.x async + asyncpg, pgvector, redis, `openai`, httpx,
structlog, numpy, tiktoken, pypdf, cryptography, PyJWT, orjson, click, prometheus-client, opentelemetry×N.
Dev: pytest, pytest-asyncio, pytest-cov, black, isort, mypy, ruff.

Positive: dependency surface is mainstream and pinned at the project level; `uv.lock` present; tests are
plain pytest (no heavy fixtures). No pinned advisory/audit step in CI is evident from the repo (see §23).

---

## 5. Deployment Topology

- **Dockerfile:** `python:3.11-slim`, `pip install .`, healthcheck `GET /health`. Single image, no `.env`
  baked in.
- **`entrypoint.sh`:** starts **two** uvicorn instances — one on `$PORT` and one on `3000` (unless already
  3000). Reasonable for AtlasFlow probing port 3000, but two workers means **two copies of module-level
  singletons** (memory store, audit system, validator Fernet key) and doubled LLM-key exposure surface.
- **docker-compose.yml:** `postgres:15`, `redis:7-alpine`, `miriam-app`. **Only** `DATABASE_URL`,
  `REDIS_URL`, `LOG_LEVEL`, `ENVIRONMENT` are wired. Thus under a stock compose deployment:
  - No `CONCENTRATE_*`/`OPENAI_*` → LLM unconfigured (`/health/ready` → `llm: unconfigured`).
  - No `JWT_SECRET`/`SECRET_KEY`/`ENCRYPTION_KEY` → **defaults apply** (see §7).
  - No `ALLOWED_ORIGINS` → `*`.
- **otel-config.yaml** present; tracing only engages when `OTEL_ENDPOINT` is set.
- `docker-compose.dev.yml` exists for local dev.

---

## 6. Configuration Audit (`config/settings.py`)

| Setting | Default | Verdict |
| --- | --- | --- |
| `SECRET_KEY` | `"change-me-in-production"` (`:16`) | P0 candidate if shipped |
| `JWT_SECRET` | `"change-me-in-production"` (`:59`) | P0 candidate if shipped |
| `ENCRYPTION_KEY` | `""` (`:58`) | P3 — validator falls back to a **random per-process key** (§11) |
| `ALLOWED_ORIGINS` | `"*"` (`:64`) | P2 with `allow_credentials=True` |
| `MAX_DAILY_TRANSFER` / `MAX_TRANSACTION_AMOUNT` | 10000 / 5000 (`:86-87`) | used correctly by SafetyPolicy |
| `AUTO_APPROVE_THRESHOLD` | 100.0 (`:88`) | **unused** — SafetyPolicy hardcodes 100.0 internally (§10) |
| `JWT_EXPIRATION_MINUTES` | 60 | fine for chat sessions |
| Concentrate block | gateway-first routing, `gpt-5.6-terra` etc. | fine |
| `PROACTIVE_ENABLED`, `ONBOARDING_ENABLED` | true | feature flag gates exist |
| `DOCUMENT_*` | new WIP block | uncommitted; reviewed as context only |

`.env.example` mirrors the defaults (including `JWT_SECRET=dev-jwt-secret-change-in-production`) and documents
the Concentrate + Supermemory blocks. Good practice overall; the issue is that **bare `Settings()` already
constitutes a permissive production config**.

---

## 7. Identity & JWT Trust Model — **P0/P1 findings**

Chain of trust and where it breaks:

- Go signs HS256 JWTs with `JWT_SECRET`. Python decodes with the same secret
  (`auth/jwt.py:18-33`), algorithm locked to HS256.
- `get_current_user` builds the `User` from claims **only** (`dependencies.py:50-57`) — `is_active=True`
  unconditionally, no revalidation against Go, no revoked-token check.
- Roles are derived from claims with a promotion rule: `verified: true ⇒ role "verified"` and any
  `role`/`roles` string is accepted (`dependencies.py:60-80`); default fallback is `["user"]`.
- `ROLE_LEVELS` then maps `verified ⇒ {read, plan, execute}` (`rbac.py:16-20`), unlocking every money tool.
- `SecurityPolicy._is_action_allowed` gates mutations to an allowlist, but the allowlist itself is a
  curated set that includes all real money tools (`policy.py:172-192`).

**Findings**

- **P1 (critical if defaults ship) — Forgeable session → arbitrary money within limits.** With
  `JWT_SECRET` at its default, any party who can reach `/api/v1/chat` can self-mint
  `{sub: victim, verified: true, role: verified}` tokens and drive `approved_actions` through the gate
  (§9). `docker-compose.yml` does not override it. Even outside the default-secret scenario, `create_token`
  (`jwt.py:36-52`) means the Python process *is* the minting authority — a single RCE/SSRF in the agent
  yields the whole identity layer. **Verified: default secret + role-from-claims + minting capability.**
- **P1 — No server-side revocation / activity check.** Claim-built users are never confirmed live with Go;
  a stolen or livetime token continues to pass even after a balance/hold/freeze changes at the source.
  UNKNOWN whether Go middleware on the downstream calls compensates (Go is not in this repo).
- **P3 — `create_token` is currently latent** (only re-exported at `auth/base.py:3-7`, no callers). Downgrades
  severity today, but it is a foot-gun on a money system.

**Recommendation:** (a) generate a per-deployment secret and fail startup if `JWT_SECRET` equals a known
default; (b) remove or gate `create_token` behind an internal-only secret; (c) revalidate the user with Go
at high-value actions (or rely on Go for every mutation call, which is the current design); (d) consider
asymmetric signing (Go private key / Python public key) so Python can never mint.

---

## 8. RBAC & Authorization

`auth/rbac.py` is clean and registry-driven: `_load_tool_permissions()` populates mutation tools ⇒
`execute`, read tools ⇒ `read`, unknown ⇒ `read` (`rbac.py:28-48`). `require_tool_access` is invoked inside
`_safe_execute` for **every** tool execution (`agent_loop.py:562-563`), so RBAC is enforced in-process, not
trusted to the prompt. Roles resolve via `ROLE_LEVELS` with highest-permission-wins.

**Findings**

- **P1 — Streaming path always evaluates as `guest`.** `stream_run` calls `_safe_execute(..., user_id, None)` at
  `agent_loop.py:357-358, 455-457, 490-491` — `user_context=None` ⇒ `roles={"guest"}` (`agent_loop.py:562`)
  ⇒ every money tool returns "User does not have permission" (`rbac.py:69-72`). Verified versus unverified
  doesn't matter; **no money action can ever execute over `/chat/stream`**, even after the user approves.
  Non-streaming `/chat` passes real `user_context` (`chat.py:264`) and works. Impact: any client that
  confirms via streaming is silently broken; the failure surfaces as an error tool result, not a clear block.
- **P2 — Roles ride entirely on the (forgable) claim, not on a verified signal** — see §7.
- **P3 — Some Go-writing tools are not marked `is_mutation` and auto-execute without confirmation:**
  `create_obligation` (`definitions.py:624-661`), `mark_obligation_paid` (`definitions.py:664-682`),
  `save_bill_beneficiary` (`definitions.py:818-852`). They don't move money, but they *do* mutate Go state
  with no staged approval and no audit `_MONEY_TOOLS` recording. Decide whether that is intended.

---

## 9. The Staged-Confirmation Model — **P1**

This is the heart of the system, so it is examined in detail.

**How it works (verified in `agent_loop.py`):**
1. LLM proposes tool calls; `run()` splits them (`agent_loop.py:204-284`). Any tool with
   `is_mutation or requires_approval` is **staged**, never auto-run, unless its `(tool,args)` signature
   appears in `approved_actions`.
2. Staged actions become `ProposedAction` cards (`agent_loop.py:263-270`), the turn returns
   `requires_confirmation=True` and the UI shows a confirmation card (`_summarize_action` builds a
   human-readable summary, `agent_loop.py:697-735`).
3. The client re-submits `/chat` with `approved_actions` in the body (`chat.py:181`). `run()` pre-executes
   them **eagerly, before any LLM call** (`agent_loop.py:142-168`), then lets the LLM narrate from cached
   results. Within-turn idempotency is handled by a signature cache (`executed_results`).
4. Signature matching is type-tolerant (100 vs 100.0) and normalized recursively (`agent_loop.py:656-666`);
   `_matches_pending` uses the same signature set (`agent_loop.py:672-683`).
5. Deterministic idempotency key `miriam:<tool>:<sha256(user,tool,args)>` is attached to mutation context
   (`agent_loop.py:583-586`, `644-654`) and forwarded to Go. Good.
6. **Investment two-phase (auto-replay):** when Go answers a mutation with HTTP 202 `AWAITING_CONFIRMATION`
   + payload-bound `confirmation.token`, `_replay_staged_confirmation` (`agent_loop.py:590-642`) automatically
   re-invokes Go with the token — **unless** the policy verdict is `REQUIRES_AUTHENTICATION` (in-app passcode),
   in which case it refuses. So one user approval ⇒ preview+token from Go ⇒ immediate token replay ⇒ executed.

**Findings**

- **P1 — The confirmation invariant is client-trusted, not server-recorded.** There is no pending-action
  ledger, no nonce, no expiry, no server-side binding of "this approved action == the proposal we showed
  you". `approved_actions` is opaque request-body input. The docstring invariant "attached to the exact
  proposed action" (`agent_loop.py:13-14`) is realized only as a signature match against
  client-sent values, and it holds only *within* the request for duplicates — there is **no cross-request
  server state** proving a proposal existed. Attack/bug surface: a buggy or malicious client sends
  `approved_actions` for an action Miriam never proposed (amount/recipient altered between what the user
  saw and what is replayed); the alteration is invisible to Miriam because nothing pins the two.
  **Mitigation today:** JWT possession (§7), RBAC role, SafetyPolicy limits (§10), Go-side ledger/OTP.
  **UNKNOWN:** whether Go binds OTP to amount+recipient on every mutation — if it pins only to "user
  approved something", the amount-swap risk is real.
- **P1 — Auto-replay means the agent is the one executing confirmation for investments.** Because the token
  returned by Go is replayed by the *agent* (not by a fresh user gesture), the only human confirmation for a
  Glider investment is the single pre-approved action. Combined with (a), this is a two-party handshake where
  one party is the client. If Miriam emitted the right preview, this is fine; if any layer buffers the
  payload between proposal and replay, it is not detected.
- **P2 — No tests whatsoever cover `approved_actions`** — grep of `tests/` returns zero matches. The single
  most security- and money-critical code path is untested (see §24).
- **Positive:** within-turn double-execution is hard-blocked by the signature cache + deterministic
  idempotency key; failed approved actions are not marked executed so a retry can fire (`agent_loop.py:
  249-261`); RBAC+safety run before every execution (`_safe_execute`), including eager pre-execution.

**Recommendation (lowest-risk fix):** mirror the proposed action and the approved action in Go as a
server-held "confirmation session" — Go returns a signed `confirmation_id` bound to `(tool, args-hash,
user, exp)` at proposal time, and only accepts an approved action that carries that id and re-verifies the
payload hash. That moves the invariant from "client round-trip" to "ledger state".

---

## 10. `SafetyPolicy` Deep Review (`safety/policy.py`) — **P2**

The file is 893 lines and has had genuine bug-fixes (limits now read from settings; recent-activity now
reads real audit rows). Residual issues:

- **P2 — Boolean used as a money threshold.** `_load_approval_workflow` sets
  `required_for_large_amounts: True` (`policy.py:86`). `_requires_approval` then evaluates
  `amount > True` ⇒ `amount > 1` (`policy.py:613-615`) — i.e. *anything above $1 "requires approval"*. The
  branch that actually gates approval is `validate_action`'s `if requires_approval:` block, which only
  **logs** (`policy.py:125-138`) and never denies. Net effect: the approval policy is vestigial — real
  staging is done by the tool-loop metadata (§9), which the policy does not interoperate with. The config
  says one thing, code does another.
- **P2 — Dead subchecks.** `tool_name in approval_workflow.get("require_approval", [])` always false because
  the dict has no such key (`policy.py:603-604`). `auto_approve_below_threshold: 100.0` is hardcoded
  (`policy.py:88`) while `settings.AUTO_APPROVE_THRESHOLD=100.0` exists and is unused — the class even has a
  docstring bragging that it fixed "hardcoded numbers disconnected from settings" (`policy.py:34-40`).
  `_has_failed_attempts` always false (`policy.py:815-827`); time-based restrictions always none
  (`policy.py:529-544`).
- **P2 — The suspicious-pattern engine can misfire or never fire.** `_check_pattern`'s new-beneficiary
  check reads `arguments.get("destination_account")`, but `send_money` uses `to` (`policy.py:357-360`;
  `definitions.py:218`) — so that pattern never triggers on real sends. `_check_multiple_large_transfers`
  requires `activity.get("type") == "transfer"` and ≥ **3** transfers ≥ threshold in an hour
  (`_get_recent_activities` labels everything "transfer", `amount ≥ threshold` is compared against
  threshold=3 count — logic is threshold-vs-threshold at `policy.py:326-334`, mixing count with amount).
- **P2 — Limit checks silently evaporate when the audit DB is down.** `_get_recent_activities` fails open to
  `[]` (`policy.py:752-794`); the daily-limit sum then sees 0 activity (`policy.py:482-484`). Note (correction
  to an earlier draft of this finding): the audit logger is **not** `None`-forever on first-boot failure —
  `get_audit_system_singleton` re-runs its init on every request (`dependencies.py:126-138`), so it recovers
  once the DB is back. The real money bug is the fail-open read: while the trail is down, limits see "no
  activity" and the daily cap silently unbinds.
  Fail-open here means **fail-permissive for money**, which contradicts the module's overall fail-closed
  posture elsewhere — fixed by making the daily-cap check strict (deny on unreadable trail).
- **P2 — Risk scoring is stubbed with stale tool names.** `_get_tool_risk_level` keys on legacy names
  (`transfer_funds`, `withdraw_funds`, `deposit_funds`, `execute_strategy`, `get_balance`, … —
  `policy.py:674-690`) that no longer exist in the registry ⇒ every current tool gets the 0.5 default.
  `_get_user_risk_level` returns a constant `"medium"` (`policy.py:692-704`, by design, with an honest
  comment). Net: the "high-risk tier" limit branches (`policy.py:468-475`) are unreachable today.
- **P2 — Float money throughout.** `_to_money` (str/int/float → float, fails to 0.0, `policy.py:7-17`);
  limits, patterns, and sums all use floats. NGN amounts ride as projected floats (< 2^53, tolerable) but
  this is the wrong primitive for money; Go already serializes amounts as strings.

**Positive:** the allowlist is registry-derived and curated (`policy.py:160-204`); `_check_limits` correctly
handles `amount_ngn` for bills; block-on-exception posture in `validate_action` is deny-safe
(`policy.py:152-158`); the blocked-categories substring check works.

---

## 11. Input Validation & Rate Limiting (`safety/validator.py`)

- Rate limits: chat 60/min, transaction 10/min, auth 20/min (Redis-backed). Enforced at `/chat` and
  `/chat/stream` (`chat.py:189-193, 327-331`). Transaction endpoint rate limit exists in the validator but
  is only documented — there is no `/transaction` endpoint in this repo (Go owns execution).
- User-input validation: length/content checks + a Fernet-encrypted field path; module-level `_validator`
  is built once (`chat.py:42`).
- **P3 — Random Fernet key when `ENCRYPTION_KEY` unset.** `InputValidator` generates a key at import if the
  env var is missing (verified pattern), so any encrypted data becomes undecryptable after restart, and
  encrypted fields are not stable across two uvicorn workers (`entrypoint.sh` runs two!). Meanwhile
  `core/security.py` derives its Fernet key from `SECRET_KEY` deterministically — **two encryption paths
  with different key-derivation semantics** in the same codebase. Align them (use `core.security`).
- P3 — Rate-limit buckets are process-local in Redis? (verified Redis-backed) — fine; but the two-worker
  entrypoint doubles the effective chat allowance unless the Redis key space is shared (it is, by user id).

---

## 12. Audit Trail (`safety/audit.py`)

- `AuditSystem` owns **its own async engine**, `Base.metadata.create_all`, and a 7-year retention
  (`retention_days=2555`). Trace id is stamped into the details JSON (`_with_trace`, via
  `current_trace_id()`).
- Wiring: a single observer registered once on the shared registry (`chat.py:97-144`) logs every tool
  execution asynchronously; `_money_details` extracts amount/recipient/symbol/strategy into the durable row
  (`chat.py:65-94`) — this is what makes §10's daily-limit math possible.
- **Findings:** P2 — `dependencies.py:126-138` hands back a module-level `_audit_system` that fails open to
  `None` until the DB becomes reachable (retried on **every** request — it re-runs init per call, it is not
  `None`-forever). While the trail is down, daily limits see no activity and silently unbind (§10) —
  **fixed** by making the daily-cap read strict (money checks deny on unreadable trail).
  P2 — audit is observer-based: if the registry's `_notify` swallows errors (`tools.py:226-231`) the failure
  is invisible; there is no load-tested fallback buffer for the 7-year table. P3 — reads of the audit log are
  `get_user_audit_logs(user_id, 200)` with in-Python filtering for the last 24h — fine at this scale.

---

## 13. Tool-Calling Architecture (`agents/tools.py`)

Solid core:

- `Tool` dataclass carries `risk_level`, `is_mutation`, `requires_approval`, `allow_auto_execute`
  (`tools.py:39-52`); `to_llm_schema()` yields OpenAI-style function defs.
- `validate_args` types args against JSON Schema (with permissive str→number/`int(float())` coercion,
  `tools.py:66-104`) before every execution — the LLM can't smuggle wrong-typed amounts past the contract.
- Registry is the single source for RBAC permissions (`rbac.py:28-48`), the safety allowlist
  (`policy.py:160-204`), auto-execute vs staged sets (`tools.py:206-220`), and the system prompt tool list —
  **no drift between what the model sees and what executes**.
- Observers get a rich record (status, elapsed, trace_id, `_args`, risk) so audit/telemetry don't recompute.
- Tool results sent back to the LLM are truncated to 2000 chars (`agent_loop.py:694`).

**Findings:** P3 — `validate_args` uses a hand-rolled type checker rather than a schema library
(jsonschema); enum/format constraints are not enforced (e.g. `period`, `category` enums are descriptive).
P3 — `_notify` swallows observer exceptions silently.

---

## 14. Tool Catalog & Permission Matrix

Read-only (auto-execute, `read` role): `get_balance`, `get_transactions`, `get_document_result`,
`get_spending_summary`, `analyze_portfolio`, `get_financial_plan`, `budget_advice`, `search_memory`,
`lookup_recipient`, `list_automations`, `list_scheduled_investments`, `list_obligations`,
`list_bill_beneficiaries`, `list_bill_providers`, `list_data_plans`, `list_cable_packages`,
`detect_network`, `validate_meter`, `get_bill_payment_history`, `get_cash_flow_forecast`,
`get_financial_health`, plus `create_obligation`, `mark_obligation_paid`, `save_bill_beneficiary`
(these three write Go state but are **not** marked mutation — §8 P3).

Money / staged (`execute` role, require confirmation):
`send_money`, `transfer_stash_to_spending`, `transfer_spending_to_stash`, `pay_bill`,
`create_automation`, `update_automation`, `delete_automation`, `create_scheduled_investment`,
`pause_scheduled_investment`, `resume_scheduled_investment`, and the Glider investment tools —
`buy_asset`, `sell_asset`, `set_allocation`, `create_strategy`, `update_strategy`, `enroll_strategy`,
`pause_strategy`, `resume_strategy`, `rebalance_strategy` (`tools/investment_definitions.py`).

Notable design: `pay_bill` carries `amount_ngn` (face value); bill/strategy/investment tools all delegate
to Go REST and all carry idempotency keys; investments are staged via Go's 202 token flow and honor
`REQUIRES_AUTHENTICATION` (passcode) by refusing to auto-replay. `create_automation`'s default
`action_type=transfer_to_stash` when unparsed (`definitions.py:457-464`) is a silent default that could send
money the user didn't quite specify — P3 to require an explicit action type.

---

## 15. Money-Tool Design Review

- `send_money` requires `to` + `amount`, delegates with JWT + idempotency key (`definitions.py:198-233`).
  Recipient verification tool `lookup_recipient` exists and is cheaply injectable into the loop. Good.
  P3: SafetyPolicy's beneficiary-pattern reads `destination_account`, not `to` (§10) — the pattern check
  never sees real sends.
- `pay_bill`: category/recipient/amount_ngn; `_check_limits` correctly counts `amount_ngn` toward caps
  (policy + a regression test). Good.
- Automations / scheduled investments: confirm-on-create, future runs happen without chat (as documented).
  Persistent money automation with only one-shot approval is inherently the highest-leverage attack surface
  once mandates arrive — note for P2 when the Go side introduces them.
- **Investments (Glider) are honest:** buy/sell adjust target allocation, not limit orders; the tool
  descriptions surface UNAVAILABLE investor data; withdrawals are app-only (no agent tool). This is a
  well-bounded surface. The auto-replay nuance is §9.

---

## 16. Financial Computation Layer (`financial/intelligence.py`)

- Strings are parsed with `_first_numeric`; `_money` collapses to float; many paths fall back to `[]`/`{}`
  with `except:` logging. Allocation math guards `monthly_income == 0` (a past ZeroDivisionError was fixed).
  Auto-generated execution steps are emitted per goal type (retirement / emergency_fund / debt_paydown /
  wealth_building / income_generation) — deterministic templated advice.
- **Verdict:** by design this layer is advisory (reads + projections); money accuracy lives in Go. The float
  money and permissive `except:` are P3 here precisely because it cannot move money. The main trap to watch:
  `budget_advice`/`analyze_portfolio` return float-based numbers into the LLM prompt, which may be quoted to
  the user as fact — ground numbers in currency string formatting (see §27). Keep this layer out of the
  Go-write path permanently.

---

## 17. Memory & Vector Search

- `database/memory.py` MemoryStore: conversations/messages/memory_entries/profile, local SQL;
  retrieval by type; the docstring at `memory.py:304` admits production vector search is **not implemented**
  here.
- `MemoryEntry.embedding` is a SQLAlchemy **JSON** column, not a pgvector vector (`models.py:189-191`).
  `vector/pgvector.py` creates and queries its **own separate `embedding VECTOR(1536)` table** via raw text
  SQL — disconnected from the ORM model. So there are two distinct "vector" artifacts and neither is the
  one semantically wired to retrieval. Result: local memory is effectively keyword/type-based; **the real
  semantic recall path is Supermemory** (fail-open, `MAX_FACTS=12`, /v4/* endpoints, container-tagged per
  user).
- `database/working_memory.py`: Redis, TTL 30 min, max 20 entries, prefix `miriam:working:`, fail-open.
  Fine.
- **Findings:** P2/P3 — unify the embedding story (one table, one serializer, real vector search over
  memory_entries, or delete `vector/` if Supermemory is the product decision); document that local search is
  not semantic. P3 — Supermemory is fail-open for reads but writes are fire-and-forget; consider whether
  memory writes need retries for correctness (they currently don't).

---

## 18. LLM Providers (`agents/llm.py`, `agents/concentrate.py`)

- Provider selection: if `CONCENTRATE_API_KEY` set ⇒ Concentrate gateway (`gpt-5.6-terra`, provider-pool
  routing `performance`, fallback `claude-sonnet-5,gemini-3.6-flash,auto`, explicit cache breakpoints after
  the system message); else OpenAI. Complete() and stream() both implemented; stream yields token/tool_call
  events.
- Context budget: `DEFAULT_MAX_CONTEXT_TOKENS=12000` trimming in `trim_messages` (tiktoken `cl100k_base`,
  ~4 chars/token fallback) — good anti-overshoot.
- Max 5 tool rounds hard cap returns a safe "stopped safely" message (`agent_loop.py:521`).
- **Findings:** P3 — Concentrate is a third party the Go algorithm can't verify; its `gpt-5.6-terra` routing
  with model fallbacks means the *personality* may shift under load (inconsistent tone is acceptable; but
  fallback ordering should be locked in config, which it is). P3 — no per-user provider quota/abuse abatement
  beyond the chat rate limit (60/min) and the tool-round cap; LLM cost can explode if a client loops.

---

## 19. Prompt & System-Prompt Engineering (`agents/system_prompt.py`, `agent_loop.py`)

- `build_system_prompt` layers personality + user context + memory facts + financial plan (injected per
  request). `_load_financial_plan` fetches live from Go, fail-open (`chat.py:586-600`).
- Money rules are stated in the prompt (never auto-run money; summarize; demand confirmation), but as §9
  shows the *enforcement* is metadata-driven — good defense, since prompt-only constraints are not
  guarantees.
- Memory facts and plan snippets are trusted into the prompt without source labeling; an attacker who can
  write Supermemory facts for themselves (via chat) could steer the model's reasoning about their own
  finances. Not a money-risk because execution remains RBAC+gated, but worth a P3 note (label provenance).

---

## 20. Onboarding Subsystem (`onboarding/service.py`)

- Full state machine (interview → polls → statement → plan → consent), single function per inbound message,
  deterministic transitions, guard-rail cap (max 12 questions / 3 follow-ups), warm fallback on LLM failure.
- **Verified — the plan-present turn has an adversarial clamp (spec §30):** any reply the LLM generates that
  invents figures not present in the deterministic plan is never shown; the turn is clamped, the violation
  tagged (`_spec_violations`), and the grounded plan embedded (`grounded_extra=json.dumps(plan)`,
  `prompt_version`). `aha_generated` fires once per fresh plan. This is genuinely strong work.
- Consent mapping: yes → standing rules; no → draft; adjust → rework. Onboarding owns turns unless
  `approved_actions` present (an OTP replay skips onboarding — correct per `chat.py:213`).
- Evaluations exist (`tests/test_evals_onboarding.py`, `test_onboarding.py` 66 tests) — the most-tested area
  of the app.

---

## 21. Proactive Analyst (`api/proactive.py`)

- Go's reacher worker calls `POST /api/v1/proactive/analyze`. `period` is regex-pinned to a fixed enum
  (`last_90_days|last_6_months|last_12_months|this_month|last_month`). Decisions fail open to "stay quiet."
- Per `docs/P1-BLUEPRINT.md`, proactivity is messages-only with OTP for money; T2 mandates are P2. The
  Go-side pre-gate (caps, quiet hours, dedupe) is authoritative. This boundary is correct for P1.

---

## 22. Observability

- **Logging** (`observability/logging.py`): structlog JSON, key/value contextvars.
- **Metrics** (`observability/metrics.py`): Prometheus counters/histograms exist (requests, latency, LLM
  calls, tool executions, memory ops, `ONBOARDING_EVENTS`, `MIRIAM_AHA_DETECTED`); `setup_metrics()` is a
  **no-op**. `/metrics` serves `prometheus_client.generate_latest()` (`main.py:141-149`) — which is useful even
  with no instrument registration, but the pre-registered counters never update. **P2:** wire the counters
  (or ship them intentionally as a v2).
- **Tracing** (`observability/tracing.py`): OTel OTLP gRPC when `OTEL_ENDPOINT` set; spans stamped with the
  correlation trace id.
- **Correlation** (`observability/correlation.py`): one trace id per request (`X-Miriam-Trace-Id` inbound,
  adopted-or-minted by middleware `main.py:60-75`, echoed on response), threaded into tool records, audit
  rows, onboarding traces, OTel spans. Consistent and complete — this is the best piece of cross-cutting
  infrastructure in the repo.

---

## 23. Dependencies & Supply Chain

Pinned toolchain via `uv.lock`. Libraries are mainstream. No `pip-audit`/`osv-scanner` gate visible in CI
config within the repo (CI workflow is referenced by the contract doc but not present here) — **P3.** Secrets
appear only as env-config defaults, not committed values. The two-worker entrypoint exposes the same image
twice — nothing more than a config risk.

---

## 24. Test Matrix (399 tests / 25 files)

| Area | File(s) | Tests | Coverage quality |
| --- | --- | --- | --- |
| Onboarding | `test_onboarding.py`, `test_evals_onboarding.py` | 66 + 16 | Excellent (state machine + evals) |
| Financial core | `test_financial_{allocation,diagnosis,engine,intelligence,profile,readiness}.py` | ~103 | Good (allocation/diagnosis/readiness) |
| Spec/examples | `test_miriam_spec.py`, `test_spec_examples.py`, `test_hypotheses.py` | 42 | Good |
| Go client | `test_go_client.py` | 14 | Good |
| Investment tools | `test_investment_tools.py` | 16 | Good |
| Proactive | `test_proactive.py` | 13 | Good |
| Safety | `test_safety_policy.py`, `test_validator.py` | 6 + 7 | **Thin:** covers limits/patterns in isolation; no `validate_action` e2e; no approval-workflow tests |
| Agent loop | (inside `test_smoke.py`) | ~23 | **Critical gap:** *zero* tests reference `approved_actions`; the staging/confirmation/idempotency-replay path is untested |
| Memory | `test_memory_session.py`, `test_supermemory.py` | 5 + ~many | Good |
| Aha | `test_aha.py` | 9 | Good |
| Quality/trace/traceability | `test_quality.py`, `test_trace.py`, `test_traceability.py` | ~51 | Good |
| Text utils | `test_text_utils.py` | 13 | Good |
| Documents (WIP) | `test_document_contract.py`, `test_documents_pipeline.py` | 9 + 6 | New/uncommitted |

**Biggest gaps:** (1) no tests for the confirmation/approval path or the streaming RBAC regression
(§8 P1) — the two findings most likely to bite in production; (2) no test for the `required_for_large_amounts`
boolean bug (§10); (3) no adversarial tests for forged `approved_actions` (e.g., client sends an action
never proposed); (4) no integration test that RBAC roles actually gate in `/chat` vs `/chat/stream`;
(5) `test_style/etc.` run under `sys.path` hacks (`test_safety_policy.py:19-20`), indicating modules are
imported app-package style sometimes, tests-style others.

---

## 25. Architecture Contract vs Reality (`docs/ARCHITECTURE-CONTRACT.md`)

The contract is aspirational and mostly honored:

- Layered direction is respected in practice; the three **grandfathered** violations are real:
  `tools/ → agents` (investment_definitions imports), `tools/ → financial`, `integrations/go_client →
  financial` (§2 of the contract) — still present and documented as refactor debt.
- AST rules (§4) are **not implemented** yet — `tests/architecture/` is referenced by the contract but does
  not exist in the repo (no `tests/architecture/` dir). No `importlinter` config/`tox.ini` found.
- Blob-size ratchet (§4.3): implied by the <?700 LOC rule; `safety/policy.py` (893), `agent_loop.py` (761),
  `definitions.py` (1125), `intelligence.py` (large), `onboarding/test_onboarding.py` (64KB) all exceed it —
  no register in-repo.
- **Intent vs practice:** the contract says "safety validates before tool execution" and "channels carry no
  financial logic" — true. It does *not* cover the confirmation-model gap (§9); the contract's stated trust
  model ("Go disposes at the boundary") actually *assumes* the §9/§7 mitigations exist on the Go side.

---

## 26. Fail-Open / Fail-Closed Posture Review

| Dependency | Read path | Money path | Verdict |
| --- | --- | --- | --- |
| Go backend | fail-open (context/plan omitted) | Go IS the ledger — if down, mutation tools error to the user (correct) | OK |
| Supermemory | fail-open → local store | n/a | OK |
| Audit DB | audits skipped | **daily limits also skipped** (§10) | **P2** |
| LLM provider | fail-over Concentrate→OpenAI; onboarding warm fallback | n/a | OK |
| SafetyPolicy internal error | block (`validate_action:152-158`) | covers money too | OK (deny-safe) |
| Streaming path | works | **always denied (guest)** (§8) | **P1** |
| JWT secret default | — | forgeable (§7) | **P0 candidate** |

The dominant theme: **read paths fail open (correct), but two money-affecting controls are wired to be
invisible when degraded (audit-db ⇒ limits) or off by default (default secret).**

---

## 27. Compliance, PII & Data Protection

- Local DB stores usernames/emails/full_name, memory entries (free text), conversations, financial profile
  data, audit details including amounts/recipients. 7-year audit retention is a sensible fraud posture but
  must be paired with an explicit retention/deletion story for conversations + memory *user deletion flows
  are not visible* — **P2** to confirm: is there a "delete my data" path (e.g., EU/GDPR-style erasure)?
- Encryption: `core/security.py` Fernet is deterministic from `SECRET_KEY`; `validator.py` uses a random key
  at boot. Two schemes, one of which loses data across restarts — align (P3).
- Amounts are floats in Python but strings at the Go boundary; rendering to users must use currency
  formatting (never raw float repr) — the number-formatting guideline applies to every tool return that
  reaches the prompt (P3).
- No secrets in repo; `.env.example` documents placeholders (good).

---

## 28. Migration & Fix Plan

| # | Severity | Fix | Effort | Where |
| --- | --- | --- | --- | --- |
| 1 | P1 | ✅ Server-held confirmation ledger: pending money actions are staged (user-id bound, args-hash keyed, 30min TTL); only approved actions that still match the staged proposal execute; forged/edited/replayed `approved_actions` denied | S (Py) | `safety/confirmations.py`, `agent_loop.py` |
| 2 | P1/P0 | ✅ Refuse default/empty secrets in production at startup (`model_validator`) and in `create_token`; compose requires `JWT_SECRET`/`SECRET_KEY` passthroughs | S | `config/settings.py`, `auth/jwt.py`, `docker-compose.yml` |
| 3 | P1 | ✅ Real `user_context` forwarded to **all** `stream_run` `_safe_execute` calls (incl. eager path); streaming RBAC regression fixed | S | `agent_loop.py` |
| 4 | P2 | ✅ `required_for_large_amounts` → numeric `APPROVAL_REQUIRED_ABOVE` (settings-sourced); `requires_approval` now actually denies when set | S | `safety/policy.py`, `config/settings.py` |
| 5 | P2 | ✅ Daily-limit reads strict: deny when audit trail unreadable (audit DB/Redis down) — money limits fail closed | S | `safety/policy.py` |
| 6 | P2 | ✅ Metrics wired: `record_llm_call`/`record_tool_execution` + request middleware; LLM/tool/request paths increment & observe | S | `observability/metrics.py`, `agents/llm.py`, `agents/concentrate.py`, `agents/tools.py`, `api/main.py` |
| 7 | P2 | ✅ Confirmation-path tests (forge/swap/replay expiry, RBAC) + streaming-money RBAC test + settings guards + numeric-threshold tests | M | `tests/` |
| 8 | P2 | ⏳ Decide NGN/float: carry amounts as Decimal/strings in Python safety math; unify `amount`/`amount_ngn`/`amount_usd` extraction (policy/safety now reads all three where available; categorization work remains) | M | `safety/policy.py`, tools |
| 9 | P3 | Unify the vector story: one embedding table (pgvector) wired to retrieval, or delete `vector/` and commit to Supermemory | M | `database/models.py:189-191`, `vector/` |
| 10 | P3 | Implement `tests/architecture/` AST+import-linter gates pledged by the contract; add blob-size register | S | repo CI |
| 11 | P3 | Mark `create_obligation`/`mark_obligation_paid`/`save_bill_beneficiary` as intended auto-exec (document) or promote to mutation | S | `tools/definitions.py` |
| 12 | P3 | Align validator's Fernet key with `core/security.py`; document rate-limit semantics; add data-erasure flow for PII | M | `safety/validator.py` |

---

## 29. What I Would Fix First (Top 10)

1. ✅ **Kill the default JWT secret (release gate).** Production startup asserts `JWT_SECRET`/`SECRET_KEY`
   are set and not the placeholders; `JWT_SECRET == "change-me-in-production"` in production refuses to mint
   tokens. `docker-compose.yml` requires the vars via `${VAR:-}` passthroughs (empty ⇒ boot blocked). (P1/P0)
2. ✅ **Make confirmation a server fact.** Python-side pending-confirmation ledger binds the proposed action
   to `(user_id, tool, args-hash)` with a 30-minute TTL; approved actions are re-verified against it and
   consumed on successful execution, so client-replay/swap/forge is denied server-side. (P1)
   (Variant of the Go-signed `confirmation_id`; implemented Python-side to avoid a Go cross-repo change.)
3. ✅ **Fix the streaming RBAC regression.** Verified users can execute money over `/chat/stream`; the stream
   path now passes real `user_context` to every enforcement point (approval + RBAC). (P1)
4. ⏳ **Neutralize the dormant minting capability** — `create_token` is now guarded against the default secret
   in production but the dedicated-signing-key split / removal option remains open. (P1/P3, partial)
5. ✅ **Repair the `required_for_large_amounts` boolean** — replacement numeric `APPROVAL_REQUIRED_ABOVE`
   (settings-sourced); `requires_approval` now actually denies at the policy layer. (P2)
6. ✅ **Stop audit/limits from silently unbinding** — daily-cap reads are strict: money checks deny when the
   audit trail is unreadable (DB/Redis down); audit init retries per request rather than caching `None`. (P2)
7. ✅ **Write the money-path tests** that didn't exist (approved-forge, action-swap, replay, streaming RBAC,
   numeric threshold, settings guards) — 17 new tests. (P2)
8. ⏳ **Unify money representation** (Decimal/string at the Python boundary; single amount field
   normalization); policy/safety now reads `amount`/`amount_ngn`/`amount_usd` across checks. (P2/P3, partial)
9. ✅ **Wire metrics** — LLM calls, tool executions, and requests now increment/observe the registered
   counters; `/metrics` reflects live activity instead of registers-only. (P3)
10. ⏳ **Finish the architecture contract gates** (AST + import-linter + blob register) that the docs already
    promise but that don't exist in CI. (P3, unchanged)

**Bottom line:** The design is fundamentally sound — registry-driven metadata, Go-authority delegation,
stringent onboarding grounding, trace-id discipline, and a strong test culture. The three P1 findings
(confirmation model, JWT defaults/minting, streaming RBAC) are each a small, mechanical fix; none requires a
rewrite. I would not ship real-money traffic with (1)-(3) open, but the foundation more than justifies
continuing development on it.

---

## Addendum — Implemented Fixes (2026-09-17)

This addendum records the fixes applied in response to the findings above. Each item maps to the relevant
section; `✅` = implemented and covered by tests, `⏳` = partial/outstanding.

### P1 — Confirmation model (§6, §17, §27; plan row 1)
- New module `miriam_agent/safety/confirmations.py`: a server-held **pending-confirmation ledger**.
  - `stage(user_id, tool_name, args)` persists a pending record keyed
    `miriam:confirm:{user_id}:{sha256(args_signature)[:40]}`, capturing `tool_name` + the exact args
    signature (default wrapper is `_resolution`-aware). TTL 30 minutes.
  - `validate(user_id, tool_name, args)` returns a token **only** when the staged record still matches —
    i.e. the approve-time proposal. Forged, edited, or expired signatures are denied.
  - `consume(user_id, signature)` deletes the record; the confirmations are **fail-closed** (any Redis or
    record error ⇒ deny).
  - Redis-backed with an in-memory fallback (single-process; enforce `redis_enabled=True` for multi-worker
    deployments). Singleton exposed as `get_pending_confirmation_store()`.
- `Agent` now stages every proposed mutation in the proposal path (`run` and `stream_run`) and, when an
  approved action arrives, executes **only** via `validate`-checked, `consume`-on-success semantics
  (`agent_loop.py::_safe_execute`). Consumption happens only after a completed execution, so a failed Go
  call can be retried within the TTL without loss.
- Attack shapes closed by `tests/test_approved_actions.py` (7 tests): client-side forged `approved_actions`
  (`_err_forged`), action swap (approve A, request B ⇒ denied), replay after completion, expiry
  (`approved_at` older than TTL), non-approved execution of `requires_approval` tools (blocked), approved
  execution still passing RBAC + policy, and idle-path confirmation store parity.
- `tests/test_confirmations.py` (6 tests): stage/validate/consume happy path, mismatch & missing-record
  denial, expiry, consume idempotency, and the Redis-disabled (memory) mode used by the suite.

### P1/P0 — JWT default secret & minting (§14, §21; plan rows 2,4)
- `config/settings.py` gained a `model_validator(mode="after")` production guard: when
  `ENVIRONMENT == "production"`, an empty, `<32`-char, or placeholder (`change-me-in-production`)
  `JWT_SECRET`/`SECRET_KEY` raises at startup.
- `auth/jwt.py::create_token` refuses to sign when `ENVIRONMENT == "production"` and the secret is the
  default/empty — the dormant minting capability is neutralized at the point of use (P1/P3, partial: the
  separate signing-key split remains optional).
- `docker-compose.yml` now forwards `JWT_SECRET`, `SECRET_KEY`, `ENCRYPTION_KEY` from the host env
  (`${VAR:-}`); compose already ships `ENVIRONMENT=production`, so a stock boot fails fast until real
  secrets are exported — intended fail-closed behavior. Covered by `tests/test_settings_guards.py` (4 tests)
  and an existing JWT round-trip test (default-secret path only under non-production env stays green).

### P1 — Streaming RBAC (§9; plan row 3)
- `stream_run` previously passed `user_context=None` to money-path `_safe_execute` calls, silently
  downgrading verified users to guest. All stream/eager execution call sites now forward the real
  `user_context` (plus `approved` when an approval is supplied). Verified by an end-to-end test that muted
  users cannot stream money and approved verified users can.

### P2 — Approval workflow & limits (§10, §12; plan rows 5,6)
- `_load_approval_workflow` now reads a settings-sourced **numeric** `APPROVAL_REQUIRED_ABOVE` (default
  2000.0) replacing the boolean `required_for_large_amounts`; `requires_approval` recognition stays
  registry-driven.
- `validate_action(..., approved=False)` **denies** actions whose workflow requires approval and carry an
  amount above the threshold — the previous branch only inserted an "awaiting confirmation" *assistant*
  phase and never denied (mirroring the earlier confirmation bypass). A `_blocked` marker is returned so
  the agent can visibly refuse.
- Money-scoped, not global: suspicious-pattern and limit checks now apply only to tools that are mutations
  or require approval — read-only tools keep working even during an audit outage.
- Daily-cap and anomaly reads became **strict**: when the audit trail (DB/Redis) is unreadable, money
  checks **deny** instead of silently seeing "no activity" and unbinding the cap (fail-closed per-module
  posture). This supersedes the audited fail-open `_get_recent_activities` for money paths; the audit
  singleton itself always retried init per request — it was never `None`-forever (corrected in §10/§12).
- Amount extraction: policy/anomaly checks now read `amount`, `amount_ngn`, and `amount_usd` (whichever the
  tool carries; bills use `amount_ngn`) and beneficiary checks fall back to the `to` field. Full
  Decimal/string unification remains outstanding (⏳ plan row 8).
- Covered by `tests/test_safety_policy.py` (12 tests incl. new approval/deny, numeric-threshold, strict
  fail-closed, and beneficiary cases) and `tests/test_investment_tools.py` (16 tests, updated to the
  stage-then-approve flow).

### P2 — Metrics (§22; plan row 6)
- `observability/metrics.py` now exposes `record_llm_call(model, status, latency_seconds=None)` and
  `record_tool_execution(tool, status)` — both never raise so observability can't break the request path.
- Instrumented: LLM `complete`/`stream` (OpenAI provider), `concentrate._request_json`, tool `execute`
  (success/error), and a FastAPI middleware that increments `REQUEST_COUNT` and observes
  `REQUEST_LATENCY`. `/metrics` now reflects live traffic.

### Verification summary
- New/updated tests: 39 targeted tests pass (confirmations 6, approved-actions 7, settings-guards 4,
  safety-policy 12, investment-tools 16 re-run green, proactive 13, smoke included in the full run).
- Full suite (excluding the other developer's in-flight `test_documents_pipeline.py`, which cascades
  unrelated collection failures): **431 passed, 3 failed, 13 skipped, 5 errors** — the 3 failures and 5
  setup errors are pre-existing and independent of this work (`test_miriam_spec` transaction-class drift on
  `send_money`; `test_traceability` missing `mocker`/`client` fixtures and a trace-id length-cap mismatch).
- `ruff check` clean on all changed files; `mypy` reports the same pre-existing errors as the untouched
  equivalents (no new ones introduced).