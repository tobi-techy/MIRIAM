# Handoff 01 — Go Service (RAIL_BACKEND) Cleanup: Remove In-Go AI + Full Alpaca Removal

**Date:** 3 Oct 2026 · **Owner:** Tobiloba
**Repo:** `github.com/tobi-techy/RAIL-BACKEND-SERVICE` · **Local:** `/Users/tobi/Development/RAIL_BACKEND`
**Baseline:** `origin/main` @ `d9a6242a` ("Merge branch 'fix/guest-interview-handoff' into main").
Local branch at handoff time: `m0/stop-the-bleeding` @ `a09d4492`, `git rev-list --count HEAD..origin/main` = **0** → already up to date. Still, start with §1.
**Python counterpart:** MIRIAM repo is the main agentic codebase from here on. Go keeps money rails, ledger, wallets, funding (non-Alpaca), KYC, webhooks (non-Alpaca), persistence, workers that move money, and the **Python bridge** (`internal/infrastructure/platform/*`, `python_agent_client`, `python_confirm_store`, proactive reacher delivery).
**Companion:** `docs/PYTHON-REFACTOR-HANDOFF.md` (Handoff 02, MIRIAM repo).

---

## 1. Start here (pull main first — mandatory)

```bash
cd /Users/tobi/Development/RAIL_BACKEND
git fetch origin
git checkout main && git pull --ff-only origin main
git checkout -b chore/go-ai-alpaca-cleanup   # do ALL work on this branch
git rev-parse origin/main   # record SHA in the PR description
go version                  # repo is go 1.25.4 / toolchain go1.25.5 (go.mod)
```

Do not start from `m0/stop-the-bleeding` worktree state. Rebase that work onto the fresh branch only if still needed.

---

## 2. Goal + non-goals

**Goal:** Go becomes a thin money-rails service. All agentic/AI thinking lives in Python (MIRIAM). All Alpaca brokerage code paths disappear (replaced by Glider/Solana sleeve path that already exists).

**Delete:**
- (A) In-Go AI brain: `internal/domain/services/ai/**` (152 files, ~33.7k non-test lines), `internal/infrastructure/ai/**` LLM providers, `internal/domain/services/miriam/**`, simulation harness, miriam-sim, AI workers, voice/image/AI chat handlers + routes, AI DI wiring, AI config structs, AI go.mod deps.
- (B) Alpaca end-to-end: adapters, domain services, routes, webhook handlers, worker, repos, entities, DI wiring, config, Bruno files, test scripts. (~30 tracked files + references in ~25 more.)

**Keep (do NOT delete):**
- `internal/infrastructure/platform/**` (Python delegation: `processor.go`, `proactive_coordinator.go`, `onboarding*.go`, `guest_*.go`, `chat_account_link.go`, `user_resolver.go`, `optout.go`, `humanize.go`, `receipt_vision.go`, `turn_tracker.go`) + `platform_python_delegate.go`, `platform_wiring.go`, `python_guest_completer.go`.
- `internal/infrastructure/ai/python_agent_client.go` (597 lines), `python_confirm_store.go` (140), `python_inflow_client*` — the bridge itself. Everything else in `internal/infrastructure/ai/` goes.
- Money rails: ledger, wallets, funding (Bridge/Mono/Ramp/Paj — **not** Alpaca), KYC, limits, confirmation cards, reconciliation (minus Alpaca checks), proactive_reacher **delivery** skeleton (it calls Python `POST /api/v1/proactive/analyze`; keep worker, delete any in-Go analyst/nudge logic inside it if present).
- `cmd/spectrum-bridge/` (iMessage gateway, TypeScript) — untouched. `node_modules` inside it is gitignored noise, not your problem.
- Migrations history — **never delete applied migration files**. Alpaca table removal gets a NEW migration (§6).

---

## 3. Workstream A — Delete in-Go AI (biggest cut, ~1.9MB)

### A1. Domain AI brain (delete whole tree)

```
git rm -r internal/domain/services/ai
# 152 files incl. orchestrator_*.go, autopilot_service.go, anomaly_engine,
# emotion_detect.go, system_prompt_v2.go, context_assembler.go, tools/* (registry,
# execution, funding, gameplay, bank_statement, portfolio, spending, travel…)
```

Also delete the parallel "Miriam intelligence" duplicate:

```
git rm -r internal/domain/services/miriam
# service.go, intelligence_orchestrator.go, decision_engine.go,
# predictive_engine.go, proactive_nudge.go, signal_detector.go, voice.go, …
```

### A2. Infra AI providers (selective — bridge files stay)

```
# DELETE these:
git rm internal/infrastructure/ai/provider.go internal/infrastructure/ai/cencori_provider.go \
  internal/infrastructure/ai/elevenlabs_client.go internal/infrastructure/ai/elevenlabs_rest.go \
  internal/infrastructure/ai/tavily_client.go internal/infrastructure/ai/token_estimator.go \
  internal/infrastructure/ai/cost_guard.go internal/infrastructure/ai/realtime_client.go \
  internal/infrastructure/ai/workers_ai.go
git rm internal/infrastructure/ai/*_test.go   # cost_guard_test, realtime_client_test,
                                              # workers_ai_test, python_merge_client_test,
                                              # python_confirm_store_test, python_inflow_client_test
                                              # (re-add bridge tests in §8)

# KEEP these (the Python bridge):
# internal/infrastructure/ai/python_agent_client.go
# internal/infrastructure/ai/python_confirm_store.go
# internal/infrastructure/ai/python_inflow_client.go (if present; test file above references it)
```

### A3. Simulation + sim binary (delete whole)

```
git rm -r internal/simulation cmd/miriam-sim
# runner, harness, engine, persona, scenario, judge, graders, budget, soak,
# score, report, stub, generator, seeder, live, share, store (~188K)
```

### A4. AI workers (delete whole dirs)

```
git rm -r internal/workers/ai_insights internal/workers/autopilot_worker \
  internal/workers/spending_coach internal/workers/memory_worker \
  internal/workers/miriam_worker internal/workers/miriam_event_worker
```

**Triage, don't auto-delete:** `daily_pulse` (has `SetNudger(NewAINudger(AIProvider))` at `application.go:680` — remove the nudger call, keep the scheduler), `engagement_worker`, `growth_engine`, `opportunity_sync`, `copy_trading_worker`, `gameplay` — if their core is LLM copy, delete; if they are cron/schedulers with an AI garnish, strip the garnish and keep the cron. Record each decision in the PR.

### A5. Handlers — voice / image / AI chat / premium (delete files)

```
git rm internal/api/handlers/investing/voice_handler.go \          # 1089 lines
  internal/api/handlers/investing/voice_server_tool.go \           # 109
  internal/api/handlers/investing/image_analysis_handler.go \      # 1034
  internal/api/handlers/investing/ai_chat_handlers.go \
  internal/api/handlers/investing/premium_ai_handlers.go \
  internal/api/handlers/investing/conversation_handlers.go \
  internal/api/handlers/investing/enhanced_nudge_handler.go \
  internal/api/handlers/investing/portfolio_activity_handlers.go
git rm -r internal/api/handlers/eval 2>/dev/null  # check first; eval harness for AI
```

Keep: `market_handlers.go`, `investment_handlers.go` (they have Alpaca refs — cleaned in Workstream B, not deleted), `billpay_handlers.go`, `support_handler.go` (strip ElevenLabs refs, keep ticket logic).

### A6. Routes (surgical edits in `internal/api/routes/routes.go`)

Current AI route surface (verified 3 Oct): `aiGroup` voice endpoints (`/voice/session-token`, `/signed-url`, `/execute-tool`, `/prepare-action`, `/proactive-insight`, `/ai/voice/*`), conversation starters, image analysis, premium handlers, internal `/miriam/evaluate` cron endpoint (~lines 399–490), ElevenLabs config branches (~1521–1750). Remove:
1. `aiservice` + `infraai` + `alpacaadapter` imports (Alpaca import removed in Workstream B).
2. The whole `aiGroup` voice/image/conversation/premium block.
3. Internal `/miriam/evaluate` block (Go no longer evaluates users; Python analyst owns it).
4. `RegisterAlpacaRoutes` call (~2080) — Workstream B.
5. Webhook secret map entry `"alpaca": container.Config.Alpaca.WebhookSecret` (~1976) — Workstream B.

### A7. application.go (surgical edits in `internal/app/application.go`)

Remove in this order (file has ~2000+ lines; do not rewrite wholesale):
1. Imports: `aiservice`, `infraai`, `ai_insights`, `autopilot_worker`, `memory_worker`, `miriam_worker`, `miriam_event_worker`, `document_processor`/`statement_processor` only if §A9 deletes them (see decision below), `statement` parser import if Cencori path goes.
2. Struct fields (~124–135): `aiInsightsWorker`, `memoryWorker`, `miriamWorker`, `autopilotWorker`, `miriamEventWorker` (+ `proactiveReacherWorker` stays — delivery only).
3. Init blocks: ai_insights worker (~508–520), memory/miriam/miriam-event workers (~537–563, ~604), autopilot service+worker (~746–766), `SetNudger` call (~680–681 — keep daily_pulse scheduler, drop nudger), statement-pipeline Cencori/parser/vision block (~786–930 — see §A9), doc/statement worker registration (~863–941), autopilot adapters (~1612), statement adapters (~1944–2042), orchestrator `ExecuteToolPublic` shim (~1564–1570), `MemoryService` usage (~1640).
4. `AnomalyEngine` + `MorningPushSender`/`noopPushSender` (~723–741).

### A8. DI wiring (delete files, trim container)

```
git rm internal/infrastructure/di/ai_wiring.go          # Cencori provider + cost guard init
# Then triage (open each, delete AI-only content, keep Python-delegate content):
# agent_wiring.go, ai_action_adapters.go, core_chat_engine_adapters.go,
# execution_wiring.go, travel_wiring.go, misc_adapters.go (has both AI + Python callers),
# platform_python_delegate.go (KEEP — this is the bridge; only strip Cencori refs inside),
# platform_wiring.go (KEEP), python_guest_completer.go (KEEP)
```

`container.go`: delete fields `AIProvider` (line ~265), `AICostGuard` (~255), `AlpacaClient`/`AlpacaService`/Alpaca repos+services (~151–152, ~297–315 — Workstream B). **Keep** `ConversationRepo` (~277) — `ai_wiring.go` even notes the platform path needs it with no Cencori key.

`builders.go`: remove `alpaca *alpaca.Client` params + `AlpacaAccountRepo/EventRepo/InstantFundingRepo` construction (~167–301).

### A9. Decision needed: statement/document pipeline (Cencori-vision flavoured)

`internal/domain/services/statement/*` (TransactionParser, DocumentExtractor, OpenAIVision client, Textract extractor, S3 filestore) + workers `document_processor`, `statement_processor`/`worker_v2` are wired through a Cencori key in `application.go:786+`. Python already owns an equivalent pipeline (`miriam_agent/documents/*`: native PDF → PaddleOCR sidecar → classify → Decimal reconcile — audited 3 Oct).

- **Default: DELETE** the Go statement service + both workers if Python's 8-book intake (planned) covers the demo. This removes the last Cencori runtime call.
- **Keep only if** a live Go consumer (KYC? compliance?) reads `statement.*` outputs this sprint — grep first: `grep -rln "statement\." --include="*.go" internal/ | grep -v "domain/services/statement"`. If a keeper exists, keep the worker but replace the Cencori parser with the Textract/native path and file a follow-up to migrate the consumer to Python.

### A10. Config (trim `internal/infrastructure/config/config.go`)

Delete structs + defaults + env bindings: `CencoriConfig` (~290), `ElevenLabsConfig` (~276), `TavilyConfig` (~256), `AssemblyAIConfig` (deprecated), CostGuard AI fields (~172–203), Cloudflare-gateway-for-Cencori block, viper defaults (~1336–1369, ~1410+), env overlays for these keys. **Keep** Python-delegate config (Python agent URL, timeouts, service key) and the proactive reacher schedule config (delivery timing stays Go-owned).

### A11. go.mod (drop dead deps AFTER code compiles)

After §§A1–A10 + B, `go mod tidy` should drop at minimum `github.com/cencori/cencori-go`. Verify no other import needs it first (`go list -deps ./... | grep cencori` must be empty). ElevenLabs/Tavily have no direct go.mod deps (REST calls) — nothing to drop there.

### A12. .backup + scratch (delete)

```
git rm -r .backup   # phase1_handlers, phase2_services copies — stale, not built
# Also delete if present and untracked-but-committed scratch: *_task-def.json (repo root has
# ~10: current/final/fixed/debug/no-env/working/secure/grafana/updated/new-task-def.json),
# dump.rdb, sim-out/, tmp/, output/rail-money-video/node_modules (if tracked)
```

---

## 4. Workstream B — Full Alpaca removal (Glider/Solana is the path)

Python already guards this: `tests/test_glider_sleeve.py:843` (`test_no_alpaca_or_direct_glider_on_the_path`) fails the build on any `alpaca` reference on the money path. Go must match.

### B1. Tracked Alpaca files (delete — full list verified on origin/main)

```
git rm -r internal/infrastructure/adapters/alpaca internal/domain/services/alpaca
git rm internal/api/routes/alpaca_routes.go \
  internal/api/handlers/webhooks/alpaca_webhook_handlers.go \
  internal/workers/funding_webhook/alpaca_funding.go \
  internal/infrastructure/di/alpaca_helpers.go \
  internal/infrastructure/repositories/alpaca_account_repository.go \
  internal/domain/entities/alpaca_entities.go \
  internal/domain/entities/alpaca_account_entities.go \
  api/bruno/Webhooks/Alpaca\ Account\ Webhook.bru \
  api/bruno/Webhooks/Alpaca\ NTA\ Webhook.bru \
  api/bruno/Webhooks/Alpaca\ Trade\ Webhook.bru \
  api/bruno/Webhooks/Alpaca\ Transfer\ Webhook.bru \
  scripts/test/test_alpaca.go scripts/test/setup_alpaca_test.sh scripts/test/test_alpaca_api.sh \
  test/unit/funding/alpaca_funding_test.go
```

### B2. References inside kept files (edit — grep list from 3 Oct audit)

| File | What to remove |
|---|---|
| `internal/api/routes/routes.go` | `alpacaadapter` import; integration-handler wiring (~597–654); Alpaca assets endpoint (~1795); webhook secret map entry (~1976) + `Bridge/Alpaca` guard (~1989); `RegisterAlpacaRoutes` (~2080–2085); withdrawal `AlpacaAccountID` provider (~537) |
| `internal/infrastructure/di/builders.go` | `alpaca` import + client params + Alpaca repo construction (~16, ~167–301) |
| `internal/infrastructure/di/container.go` | `alpacaservice` + `alpaca` imports; `AlpacaClient/Service/AccountService/FundingBridge/EventProcessor/PortfolioSync` fields; init block (~549+) |
| `internal/infrastructure/di/{investment_wiring,funding_wiring,reconciliation_wiring,domain_wiring}.go` | Alpaca service/adapter wiring lines |
| `internal/infrastructure/di/ai_wiring.go` | already deleted (§A8); confirm no Alpaca import survives elsewhere in `di/` |
| `internal/infrastructure/config/config.go` | `AlpacaConfig` struct (~885), `Alpaca` field (~33, ~646), viper defaults (~1411–1416), env overlays (~2007–2023) |
| `internal/infrastructure/repositories/{deposit,user,virtual_account,instant_funding}_repository.go` | Alpaca column reads/writes (`alpaca_account_id` etc.) |
| `internal/domain/repositories/interfaces.go`, `domain/services/{reconciliation/service,checks,kyc/service,services}.go` | Alpaca method decls + check fns |
| `internal/domain/services/investing/executor.go` | Alpaca order path (Glider executor stays) |
| `internal/api/handlers/investing/{market_handlers,investment_handlers}.go` + `market_handlers_test.go` | Alpaca market/invest endpoints → delete file or strip to Glider reads (match Python `glider_sleeve.py` read-only posture) |
| `internal/api/handlers/{internal_handlers,investment_stash_handlers,common/integration_handlers,wallet/wallet_funding_handlers,wallet/deposit_handlers,auth/auth_handlers,handlers}.go` | Alpaca branches/fields per grep |
| `internal/api/middleware/{webhook_security,webhook_verification,kyc_capabilities}.go` | Alpaca secret/branch/capability |
| `internal/api/handlers/webhooks/unified_funding_webhook.go` | `alpacaHandler` field, `WebhookSourceAlpaca`, `case "alpaca"`, verify + `HandleTrade/Account/TransferUpdate` dispatch (~21–317) |
| `internal/workers/kyc_sync/worker.go` | `RetryAlpacaSync` iface + `case "alpaca"` (~35–194) |
| `internal/workers/kyc_autoinvest/worker.go` | `alpaca_account_id IS NOT NULL` gate (~211) — decide: gate on Glider sleeve binding instead, or drop gate |
| `internal/workers/rebalancing_worker/worker.go` | `PlaceMarketOrder` Alpaca impl (~32) |
| `test/unit/{virtual_account,autoinvest_service,services/portfolio_analytics}_test.go`, `test/integration/virtual_account_integration_test.go` | Alpaca fixtures/asserts |

### B3. Database (new migration, never edit history)

- Leave `migrations/064_create_alpaca_accounts_table.{up,down}.sql` untouched.
- Add `migrations/NNN_drop_alpaca_tables.up.sql` (drop `alpaca_accounts` + any `alpaca_*` tables) + matching `.down.sql` (re-create minimal schema for rollback). Number = max existing + 1 (280 `.up.sql` files at audit → likely `281_*`).
- Grep entities for the table names first; drop every `alpaca_*` table the entities reference.

### B4. Secrets rotation (post-merge, ops)

After merge: revoke/rotate `ALPACA_API_KEY`, `APCA_API_KEY_ID`, `ALPACA_API_SECRET`, `APCA_API_SECRET_KEY`, `ALPACA_DATA_API_KEY`, `ALPACA_DATA_KEY`, `ALPACA_DATA_API_SECRET`, Alpaca webhook secret. Removing code without rotating leaves valid creds for a path that no longer logs.

---

## 5. What "done" looks like (acceptance)

- [ ] `go build ./...` green; `go vet ./...` clean.
- [ ] `grep -rin "cencori\|elevenlabs\|tavily\|assemblyai" --include="*.go" internal/ cmd/ pkg/ services/` → empty (excluding the kept-word list in §7).
- [ ] `grep -rin "alpaca" --include="*.go" internal/ cmd/ pkg/ services/ test/` → empty.
- [ ] `grep -rin "alpaca" go.mod go.sum configs/ deployments/ fly.toml docker-compose.yml Makefile` → empty or commented-out with a TODO-free removal (no dangling env).
- [ ] Python bridge E2E still passes: Go can call Python analyst (`proactive analyze`), confirm store round-trip, inflow webhook verify — run the kept bridge tests + one live local turn.
- [ ] Glider invest path unaffected: seed-catalogue read + allocate prepare/confirm still compile + unit green (Alpaca removal must not touch the Solana sleeve path).
- [ ] New Alpaca-drop migration applies up AND down on a scratch DB.
- [ ] PR lists every triage decision from §A4 (kept workers + why).

---

## 6. Suggested commit stack (one PR, stacked commits)

1. `chore(go): remove in-Go AI domain + infra providers (keep python bridge)`
2. `chore(go): remove simulation harness + miriam-sim`
3. `chore(go): remove AI workers + voice/image/AI-chat handlers + routes`
4. `chore(go): trim application.go + DI + config of AI wiring`
5. `chore(go): full Alpaca removal (adapters, services, routes, webhooks, workers, repos)`
6. `chore(go): drop alpaca tables (new migration NNN) + go mod tidy`
7. `chore(go): delete .backup + scratch task-defs`

Each commit must build (`go build ./...`) — use `git rebase -i` to keep it so.

---

## 7. Keep list (do NOT touch in this PR)

- `internal/infrastructure/ai/python_agent_client.go`, `python_confirm_store.go`, `python_inflow_client*` — the bridge itself.
- `internal/infrastructure/platform/**` — Python delegation gateway.
- Money rails: `internal/domain/services/ledger/**`, `internal/domain/services/funding/**` (minus Cencori parser in §A9), `internal/domain/services/limits/**`, `internal/domain/services/kyc/**`, `internal/domain/services/reconciliation/**`, `internal/domain/services/investing/**` (minus Alpaca executor in §B2), `internal/domain/services/checks/**`.
- `internal/workers/{daily_pulse,proactive_reacher,kyc_sync,rebalancing_worker}` — keep scheduler/reacher, strip AI/Alpaca garnish.
- `internal/api/handlers/investing/{market_handlers,investment_handlers}.go` (minus Alpaca refs), `billpay_handlers.go`, `support_handler.go` (minus ElevenLabs).
- `cmd/spectrum-bridge/` — iMessage gateway, untouched.
- Migrations history — never edit or delete applied files.
- `scripts/init-db.sql` — only add a NEW alpaca-drop migration, don't edit history.

---

## 8. Risks + guards

- **Orphaned interfaces:** `MemoryService`, `ChatEngine`, `VoiceSessionRateLimiter`, `AutopilotQueue` types live in deleted packages but are referenced by kept files (`application.go:1564+`, `1640`, routes ~1546). Delete the reference WITH the type — compile after every commit.
- **DI nil-panics:** container fields deleted → every `container.X` deref must go in the same commit. `go vet` catches most; also run `go test ./internal/infrastructure/di/...`.
- **Statement pipeline (§A9):** the only AI-adjacent piece with a possible live reader. Grep before deleting; default delete.
- **Alpaca webhook 404s:** after deploy, Alpaca (if still configured server-side) will POST to a removed route → expect 404s in logs; confirm firewall/secret rotation so this is noise, not an incident.
- **No behaviour change to money math:** this PR deletes paths, changes no ledger/funding amounts. Any diff to `internal/domain/services/ledger/**`, `funding/**` (non-Alpaca), `limits/**` is out of scope — revert on sight.
