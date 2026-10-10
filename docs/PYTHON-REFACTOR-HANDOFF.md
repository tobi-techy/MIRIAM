# Handoff 02 — Python (MIRIAM) Refactor: One Brain, Slim Gods, Money-Correct

**Date:** 3 Oct 2026 · **Owner:** Tobiloba
**Repo:** `github.com/tobi-techy/MIRIAM.git` · **Local worktree:** `/Users/tobi/.factory/worktrees/088f8bc6/MIRIAM`
**Baseline:** `origin/main` @ `e2101e9` ("chore: merge origin/main"). Local branch at handoff time: `droid/i-ve-attached-the-file`. Start with §1 regardless.
**Companion:** `docs/GO-SERVICE-CLEANUP-HANDOFF.md` (Handoff 01 — Go deletes its AI brain and Alpaca; Python becomes the sole brain).
**Prior art (read before coding):** `docs/MIRIAM-AUDIT.md` (15 Python issues), `docs/MIRIAM-SYSTEM-DESIGN-ARCHITECTURE.md` (§2 mapping, gap register G21–G32, Milestones 0–1), `docs/MIRIAM-SME-PIVOT-TECHNICAL-REQUIREMENTS.md` (§§14–16 trust doctrine).

---

## 1. Start here (pull main first — mandatory)

```bash
cd /Users/tobi/.factory/worktrees/088f8bc6/MIRIAM
git fetch origin
git checkout main && git pull --ff-only origin main
git checkout -b chore/python-one-brain   # do ALL work on this branch
git rev-parse origin/main   # record SHA in the PR description
python --version && uv --version   # repo uses uv (uv.lock); pyproject.toml
```

Check what the Go cleanup needs from you: the bridge contract is `POST /api/v1/chat`, `/proactive/analyze`, `/money/inflow`, `/money/debit`, `/users/merge` (per `docs/BUSINESS-PIVOT-HANDOFF.md`) plus the confirm-store round-trip. Nothing in this handoff renames those routes.

---

## 2. Goal + non-goals

**Goal:** One brain (`orchestrator.py` owns the Hands→Judgment→Hands→Voice invariant), slim god-files, money-correct persistence, docs that match code, golden evals blocking CI.

**Do:**
- (A) Unify entry: kill or demote the second brain, extract the classifier into a tested module.
- (B) Split the four god files (`api/chat.py` 65KB, `agents/agent_loop.py` 40KB, `financial/intelligence.py` 122KB, `tools/definitions.py` 44KB).
- (C) Money-correctness fixes scoped to Python: Float→Numeric, timezone, single-ledger discipline, durable audit.
- (D) Fix or delete phantom docs (contract drift).
- (E) Golden eval sets blocking CI (classifier + money + hallucination).
- (F) Wire the documents pipeline to a real OCR sidecar (or delete the dead path).

**Do NOT:**
- Rename bridge routes or change the Go↔Python wire contract (Handoff 01 depends on it).
- Change ledger amounts/semantics — the 4-sleeve Decimal ledger in `hands/ledger.py` is correct; the fixes here are types, timezones, and process discipline around it.
- Reintroduce anything Go is deleting (no Alpaca client, no Cencori provider — Python already has none; keep it that way).
- Rewrite `orchestrator.py` wholesale — extract from it, don't rebuild it.

---

## 3. Workstream A — One brain (P0, do first)

### A1. Decide the fate of `agents/agent_loop.py` (40KB second brain)

Two modules today can both "run the agent": `orchestrator.py` (41KB, owns the Hands→Judgment→Hands→Voice invariant per `ARCHITECTURE-CONTRACT.md`) and `agents/agent_loop.py` (40KB loop + `agents/concentrate.py` 19KB gateway + `agents/llm.py`, `agents/tools.py`, `agents/system_prompt.py` 26KB).

1. Grep every import of both: `grep -rn "from miriam_agent.orchestrator\|from miriam_agent.agents.agent_loop\|import agent_loop" --include="*.py" miriam_agent/ tests/ | grep -v "def \|#"`.
2. **Default: `orchestrator.py` is THE entry.** Demote `agent_loop.py` to an executor library (imported BY the orchestrator for multi-step tool loops) or delete it if nothing outside `agents/` imports it. `concentrate.py` stays only as the thin gateway it claims to be — any judgment logic inside it moves to `judgment/`.
3. Record the import graph + decision in the PR. If deletion is risky, do the demotion (make `agent_loop` unimportable from `api/`, keep it importable from `orchestrator.py`) and file the deletion as a follow-up.

### A2. Extract the regex classifier (P0 — most-exercised, least-tested)

The intent classifier (regex/`re.compile` — matches in `orchestrator.py`, `hands/nl.py`, `documents/classify.py`, `money/text.py`) routes every turn and has no golden set.

1. Find the real one first: `grep -rn "re\.compile\|IntentClassifier\|def classify_intent" --include="*.py" miriam_agent/orchestrator.py miriam_agent/hands/nl.py | head -30`.
2. Extract to `miriam_agent/judgment/classifier.py` (new file, pure function `classify_intent(text) -> Intent`, no I/O, no LLM).
3. Add `tests/test_classifier_golden.py` with ≥100 cases (SME-flavoured: "how much did I make today", "pay my supplier ₦200k", pidgin variants, ambiguous money-vs-chat). CI-blocking.
4. `orchestrator.py` and `hands/nl.py` import it; delete the duplicated regex tables. (Design doc G21.)

### A3. Confirm the contract, then enforce it

`ARCHITECTURE-CONTRACT.md` states the Hands→Judgment→Hands→Voice invariant; `tests/architecture/test_module_rules.py` is the real enforcement (AST-based). After A1–A2, run it and add one rule: `api/` may not import `agents/agent_loop` directly (all turns go through `orchestrator.py`).

```bash
uv run pytest tests/architecture/ -q
```

---

## 4. Workstream B — Slim the gods (P1, split — never rewrite)

Rule for all splits: **move code, don't rewrite logic.** Each split = one commit, tests green before and after. Target ≤500 lines per file.

### B1. `miriam_agent/api/chat.py` (65KB → router + handlers)

1. List its routes: `grep -n "^@router\|^async def \|^def " miriam_agent/api/chat.py`.
2. Split into `miriam_agent/api/chat/` package: `router.py` (route decls only), `turn.py` (single-turn handler), `stream.py` (SSE/streaming), `deps.py` (auth/service-key extraction — merge with existing `api/dependencies.py` 7.6KB, don't duplicate).
3. Keep `api/spectrum.py` (23KB, CHANNELS imessage/whatsapp/terminal) as the channel gateway; add the **media→OCR seam** here (Milestone 1 G24/G30): accept image bytes on the WhatsApp/iMessage path and hand to `documents/pipeline.py`. Today WA is string-only — the seam is an interface + 413/415 guards, not the full OCR (that's §7).

### B2. `miriam_agent/financial/intelligence.py` (122KB → modules)

Largest file in the repo. Split by the seams already visible in its sibling files (`diagnosis.py` 27KB, `profile.py` 48KB, `allocation.py` 13KB, `hypotheses.py` 13KB):

1. `grep -n "^class \|^def \|^async def " miriam_agent/financial/intelligence.py` → group into diagnosis / profile-reads / allocation / readiness.
2. Move each group to (or merge with) the matching sibling module; `intelligence.py` becomes `facade.py`-style re-exports for one release, then delete it.
3. `readiness.py` (8.4KB) and `eligibility.py` (7KB) already exist — check for duplicated logic with the moved code and dedupe.

### B3. `miriam_agent/tools/definitions.py` (44KB registry → per-domain)

Split along the existing per-domain files (`funding_definitions.py`, `investment_definitions.py`, `money_definitions.py`, `vault_definitions.py`): move each tool def to its domain file, leave `definitions.py` as an aggregator that imports + exposes `ALL_TOOLS`. Update `tests/test_agent_tool_e2e.py` if it imports deep paths.

### B4. `miriam_agent/integrations/` — trim, don't split

- `go_client.py` (50KB) is the **bridge to Go — keep**, but audit for dead methods pointing at Go endpoints Handoff 01 deletes (AI evaluate/voice/vision helpers). Delete dead methods, keep money + confirm-store + proactive paths.
- `supermemory_client.py` (35KB): keep, but verify every write path passes through `conversational/redact.py` (3.3KB, Oct 3). Add a test asserting no raw account numbers/PII hit the wire (`tests/test_memory_redaction.py` exists — extend it).
- `crypto.py` (55 bytes) and `plaid.py` (47 bytes) are stubs — delete or implement; stubs that look like integrations are a liability.

### B5. `miriam_agent/hands/` — leave the money logic, fix the surroundings

`transfer.py` (36KB), `invest.py` (35KB), `orders.py`/`settlement.py`/`settle_legs.py` (21–25KB) are dense but audited-correct (Decimal sleeves, idempotency, confirm cards). Do **not** split for size alone. Only:
- `hands/nl.py` (15KB): consume the extracted classifier (§A2), keep the NL-to-claim parsing.
- `hands/reconcile.py` (6.5KB), `hands/shadow.py` (9KB Oct 3), `hands/execution_claim.py`/`execution_journal.py`: keep; shadow/parity tests (`tests/test_shadow_parity.py`, `tests/test_execution_journal.py`) must stay green through every commit in this handoff.

---

## 5. Workstream C — Money-correct persistence (P0/P1, Milestone 1 spine)

These are the Python-side rows of design-doc gaps G25–G29. Coordinate with Go Milestone 1 (orgs/shops/RLS lives in Go's Postgres; Python must not invent a parallel tenant model).

- [ ] **C1. Float→Numeric (P0).** `database/models.py` (13KB) declares money columns as `Float`; `hands/ledger.py` computes in `Decimal`. Audit found the mismatch. Migrate every money column to `Numeric(18,2)` (kobo-int precedent from the design doc is Go-side; Python-side rule: `Decimal` in code, `Numeric` in DB, never `float` between). There is **no Alembic** — `scripts/init-db.sql` is the schema source. Add the `ALTER TABLE` statements there AND as a dated migration file under `scripts/` so fresh + existing DBs converge. Grep first: `grep -n "Float\|Numeric" miriam_agent/database/models.py`.
- [ ] **C2. Timezone (P1).** Naive vs `timestamptz` split. Standardize: all timestamps `timestamptz`, all code `datetime.now(timezone.utc)` (`core/timeutil.py` is 622 bytes — put the helper there). Touch `database/memory.py` (31KB session/person memory) first; it has the most time queries.
- [ ] **C3. Single-ledger discipline (P0).** Two-process money minting (api + worker both importing `hands/`) is the design doc's P0 G22. Rule: only the process holding the turn imports `hands/ledger.py` for writes; all other paths (proactive analyst, reacher callbacks) are read-only. Enforce via the architecture test (§A3): fail CI if `proactive/` imports `hands.ledger` for anything but balance reads.
- [ ] **C4. Durable audit (P0).** `proactive/analyst.py` is fail-open and `hands/audit.py` (4.4KB) must not depend on Redis availability. Verify: kill Redis locally, run a money turn + an analyst tick, confirm the audit row exists in Postgres. (`core/redis_client.py` 4KB Oct 3 + `tests/test_redis_client.py` — extend with the Redis-down case.)
- [ ] **C5. No parallel tenant model.** Python reads `org_id`/`shop_id` from the Go-minted JWT/service-key context (`api/dependencies.py`, `judgment/state.py` 14KB). Do not add `org` tables in Python. If a Python query needs shop scoping before Go ships RLS, scope in the WHERE clause with the JWT claim, marked `# TODO(go-rls): drop when RLS lands`.

---

## 6. Workstream D — Docs that match code (P1/P2, G32)

- [ ] **D1.** `ARCHITECTURE-CONTRACT.md` references `miriam_agent/vector/`, `miriam_agent/investments/`, `miriam_agent/intel/`, `onboarding/evals.py`, and an `import-linter.ini` — none exist. Either implement (no) or fix the doc (yes): point at the real enforcement (`tests/architecture/test_module_rules.py`) and the real module list from §2 of the design doc.
- [ ] **D2.** `P1-BLUEPRINT.md` specifies `POST /api/v1/intel/evaluate` + `miriam_agent/intel/` + a Go scheduler push. Reality: `POST /api/v1/proactive/analyze` (`api/proactive.py` 3.4KB) + `proactive/analyst.py` + Go reacher pull. Update the blueprint's §3 API contract to the real path; keep the T0/T1/T2 autonomy tiers (still valid doctrine).
- [ ] **D3.** `/health` vs `/health/ready`: the design doc notes `/health` is green while money is 503. Add a ready check that verifies DB + Redis + Go bridge reachability; gate compose `depends_on` on `service_healthy`.

---

## 7. Workstream E — Evals that block (P0/P1, trust doctrine §§14–16)

The TRD demands golden sets; `eval/` today has `hallucination_cases.py`, `memory_eval.py`, `typesafe_deep_eval.py` but money has no blocking golden.

- [ ] **E1. Classifier golden** (§A2) — blocking.
- [ ] **E2. Money golden:** 50 ledger cases (post twice → single entry; conflicting balances → Conflicted tier, never invented; amounts in words vs figures). Extend `tests/test_hands_layer.py` + `tests/test_trust_rails.py` (Oct 3) — make them CI-required, not advisory.
- [ ] **E3. Hallucination eval:** wire `eval/hallucination_cases.py` into CI via `tests/test_hallucination_eval.py` (exists — confirm it's in the required suite, not skipped).
- [ ] **E4. 60-photo bench prep (non-blocking this PR):** collect the receipt-photo corpus the TRD's Golden-Doc-200 needs; the OCR sidecar work (§8) consumes it.

---

## 8. Workstream F — Documents pipeline: deploy or delete (P1, G30)

`documents/` (`pipeline.py` 7KB, `client.py` 1.6KB, `classify.py` 1.8KB, `extraction/`, `processors/` incl. `bank_statement.py`) exists but the PaddleOCR-VL sidecar from the TRD is undeployed — camera input has no path to ledger.

- **Default: wire the seam, defer the model.** This PR: `documents/client.py` gets a sidecar interface (bytes in → text+confidence out) with a stub backend returning `UNVERIFIED` tier; `pipeline.py` → `reconcile.py` → `hands/ledger.py` path covered by a test with a fixture receipt. The real sidecar (PaddleOCR-VL per TRD §16) is a Milestone 2 task that implements the interface.
- **Delete only if** nothing calls `documents/pipeline.py` outside its own tests — `grep -rn "documents.pipeline\|from miriam_agent.documents" --include="*.py" miriam_agent/ | grep -v "documents/"`. If orphaned AND the Milestone 2 owner won't staff it, delete the tree rather than carry dead code with a live-looking import surface.

---

## 9. What "done" looks like (acceptance)

- [ ] `uv run pytest tests/architecture/ tests/test_classifier_golden.py tests/test_hands_layer.py tests/test_trust_rails.py tests/test_shadow_parity.py tests/test_execution_journal.py -q` green.
- [ ] Full suite: `uv run pytest tests/ -q -x --ignore=tests/benchmark` green (benchmarks excluded; note any pre-existing skips in the PR).
- [ ] `ruff check miriam_agent/ tests/` clean (or the repo's configured linter — check `pyproject.toml`).
- [ ] One brain: `grep -rn "agent_loop" --include="*.py" miriam_agent/api/ miriam_agent/orchestrator.py` → empty (no API path bypasses the orchestrator).
- [ ] No `float` on money: `grep -rn ": float\|float(" --include="*.py" miriam_agent/hands/ miriam_agent/money/ | grep -iv "confidence\|score\|ratio\|test"` → empty or justified in PR.
- [ ] No phantom imports: every module named in `ARCHITECTURE-CONTRACT.md` exists; every route named in `P1-BLUEPRINT.md` §3 exists (`ruff` + a doc-link test if cheap).
- [ ] Bridge intact: `uv run pytest tests/test_go_client.py tests/test_proactive.py -q` green (Go-cleanup counterpart can land in either order).
- [ ] `init-db.sql` + dated migration converge (fresh `psql < scripts/init-db.sql` and migrated DB produce the same money-column types).

---

## 10. Suggested commit stack (one PR, stacked commits)

1. `refactor(py): extract intent classifier + golden set (one brain step 1)`
2. `refactor(py): demote agent_loop to orchestrator-owned executor (one brain step 2)`
3. `refactor(py): split api/chat.py into chat/ package + media seam`
4. `refactor(py): split financial/intelligence.py into domain modules`
5. `refactor(py): split tools/definitions.py into domain files`
6. `fix(py): Numeric money columns + timestamptz + single-ledger rule + durable audit`
7. `docs(py): fix contract drift (arch contract + P1 blueprint + ready check)`
8. `test(py): money golden + hallucination eval blocking CI`
9. `refactor(py): documents sidecar seam (stub UNVERIFIED backend)`

Each commit keeps the suite green — `git rebase -i` to keep it so.

---

## 11. Keep list (do NOT touch in this PR)

- `hands/ledger.py`, `hands/transfer.py`, `hands/invest.py`, `hands/orders.py`, `hands/settlement*.py`, `hands/settle_*.py`, `hands/funding*.py`, `hands/limits.py`, `hands/reconcile.py`, `hands/shadow.py`, `hands/execution_*` — money logic (surroundings only).
- `money/glider.py`, `tools/glider_sleeve.py`, `tools/glider_strategy_reads.py` — Glider/Solana path + its `test_no_alpaca_or_direct_glider_on_the_path` guard.
- `judgment/gates.py` (25KB), `judgment/decide.py`, `judgment/rules.py`, `safety/grounding.py`, `safety/validator.py`, `safety/policy.py` — trust rails (extend via tests, don't restructure).
- `proactive/analyst.py` + `proactive/state.py` — pull contract Go depends on (durable-audit fix only).
- `confirm_cards/`, `money/intake.py`, `money/reference.py`, `money/safety.py` — confirmation + intake spine.
- `onboarding/` (14 modules) — untouched; its own refactor is a separate handoff.
- `voice/` — untouched pending Go voice-handler deletion landing (then re-point TTS/STT if needed).

---

## 12. Risks + guards

- **Split-drift:** moving code between modules breaks deep imports in tests (`test_money_layers_route_live.py`, `test_dual_run_money_layers.py` import internals). After each split commit, run the full suite — not just the touched area.
- **Circular imports:** `orchestrator.py` ↔ `agents/` ↔ `judgment/` cycles are the classic failure when demoting `agent_loop`. If a cycle appears, the dependency direction is `api → orchestrator → judgment/hands → tools/integrations`; `agents/` must never import `api/`.
- **Float→Numeric migration on live data:** round, don't truncate; backfill in a transaction; verify `SUM()` equality pre/post on a staging snapshot before touching prod.
- **Classifier behaviour change:** the golden set (§A2) must pass against the OLD regex first (record baseline), then against the extracted module — identical results, then extend. Any intent flip on real traffic is a regression until proven otherwise.
- **Go-cleanup ordering:** either PR can land first (the wire contract is unchanged), but do not merge both on the same day — land Python first, run one live iMessage turn end-to-end, then land Go.
