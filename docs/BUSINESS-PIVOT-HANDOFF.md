# Miriam for Business: Pivot Handoff

Date: 2026-09-29
Scope: Miriam (Python agent, `/Users/tobi/Development/MIRIAM`) and Rail (Go backend, `/Users/tobi/Development/RAIL_BACKEND`)
Purpose: Decide and start the pivot from a consumer money agent to an always-on finance agent for businesses.

How to read this: Section 1 is the whole story in one page. Sections 2 to 4 are the evidence. Sections 5 to 8 are the plan. Section 9 is what to do on Monday. Section 10 lists what I could not verify.

---

## 1. Summary

**The pivot is worth doing, and the codebase gives you a real head start. But the head start is in trust and safety machinery, not in business finance features.**

What you have:
- A well-designed money-safety architecture in Miriam: deterministic "Hands" move money, typed "Judgment" decides, "Voice" only narrates, and the LLM is read-only. This is the part hardest to build and easiest to reuse.
- A production-grade double-entry ledger, idempotency, reconciliation and recovery workers in Rail.
- Working Nigerian rails: NGN virtual accounts (Graph), open banking (Mono), bank-statement parsing (Textract plus vision LLM), bill pay (Airbills), on/off ramps (Paj, RampHub), and KYC/AML (Sumsub, Didit).
- A confirmation system (Face ID / WebAuthn cards) that can grow into approvals.

What you do not have (verified absent in both repos):
- No organizations, teams, roles or maker-checker approvals. Everything is one consumer `user_id`.
- No KYB, invoices, receivables, payroll, bulk payouts, accounting sync, expense management or business cards.
- No scheduler in Python. "Always-on" today is a Go worker that calls Python every ~30 minutes.
- Rail's ledger only accepts `USD` and `USDC` (a database CHECK constraint). NGN exists only at the edges.
- The Python tree does not currently import, because of an unresolved merge conflict in `miriam_agent/config/settings.py`.

What the market says:
- Agentic finance is real and crowded in the US and global markets (Ramp, Brex, Airwallex, Puzzle, Digits, Campfire and others). Every serious player converged on the same rule: **the agent proposes, a human approves, every action is audited.** That is exactly what Miriam's architecture already does.
- In Nigeria there are about 40 million MSMEs producing about 48% of GDP, and 2026 is a forcing year for compliance (new tax act, e-invoicing, real-time AML rules). I found no clearly dominant AI-native finance agent for Nigerian SMEs. Treat that as a hypothesis to test with customers, not a fact (see section 10).

**My recommendation (opinion, not fact):**
1. Target Nigerian SMEs with 10 to 100 staff first. Add payroll and accounting bureaus as a channel in phase 2. Defer large enterprises.
2. Build a **"Finance Watchtower + Payroll Co-pilot"** as the MVP. It watches cash, payroll, tax remittances and receivables around the clock, and drafts the next action for a human to approve.
3. **Do not move money in the MVP.** Read bank data, prepare payroll and payment batches, get approvals, and export or hand off. Add execution (through a licensed partner) in phase 2, after you have design partners and legal clarity.
4. Put the new business domain in its own tables and services. Do not retrofit the consumer ledger yet.
5. Fix the merge conflict and get the test suite green before anything else.

---

## 2. What exists: Miriam (Python)

Numbers come from a read-only audit of the repo. I did not run the test suite (it cannot be collected until the merge conflict is fixed).

### 2.1 Architecture in one picture

```
User message / event
        │
        ▼
 classify_turn (keyword + regex)
        │
   ┌────┴─────────────────────────────┐
   │ money turn                       │ everything else
   ▼                                  ▼
 Orchestrator                      LLM agent loop (READ-ONLY tools,
 Hands → Judgment → Hands → Voice   max 5 tool rounds, grounding guard)
   │
   ▼
 Go rail (Rail) = money authority
 (X-Miriam-Confirm-Id / X-Miriam-Receipt-Id headers)
```

Key point: Python never moves money. The LLM can only call read tools. Money actions are parsed from the user's own words by regex, staged as a challenge with a `confirm_id`, and settled only after an explicit confirmation. Model prose is never parsed for actions.

### 2.2 Components and maturity

| Component | Path | Maturity | Reusable for business? |
| --- | --- | --- | --- |
| Orchestrator (single door) | `miriam_agent/orchestrator.py` | Working, well tested | Yes, core |
| Hands: ledger, limits, audit, receipts, confirm | `miriam_agent/hands/` (8k LOC) | Working, production-shaped, Decimal money | Yes, but needs org-scoped limits and multi-approver |
| Judgment (typed decisions, gates) | `miriam_agent/judgment/` | Working, depends on TypeSafe/JEV service | Yes |
| Voice (narrate state, clamp figures) | `miriam_agent/voice/` | Working | Yes, needs new prompt |
| Agent loop | `miriam_agent/agents/agent_loop.py` | Working | Yes |
| Grounding and reply guard | `miriam_agent/safety/grounding.py`, `judgment/egress_claims.py` | Working. Best-evidenced subsystem: 62 eval cases, reported 100% caught and 0% false positives | Yes, high value |
| Tool registry with risk metadata | `miriam_agent/agents/tools.py` | Production-ready | Yes |
| Audit trail | `hands/audit.py`, `safety/audit.py` (Postgres, 7-year retention) | Working, fail-open by design | Yes |
| Memory (Supermemory, Postgres, Redis) | `integrations/supermemory_client.py`, `database/memory.py` | Working | Partly. Needs org-scoped memory |
| Proactive analyst | `miriam_agent/proactive/analyst.py` | Working, pull-only | Yes, as the seed of "always-on" |
| Observability (structlog, Prometheus, OTel) | `miriam_agent/observability/` | Working | Yes |
| Eval harness | `eval/`, `tests/fixtures/*_golden.json` | Working | Yes, extend for business |
| Onboarding interview (12 files, 6k LOC) | `miriam_agent/onboarding/` | Working, consumer-only | No |
| Personal planning, envelopes, 70/30, diagnosis | `miriam_agent/financial/`, `money/` | Working, consumer-only, reference data is PLACEHOLDER | No |
| Glider investing and vault tools | `tools/investment_definitions.py`, `vault_definitions.py` | Working, consumer-only | No |
| iMessage gateway (Bun/TypeScript) | `apps/spectrum-gateway/` | Working for iMessage. WhatsApp is a name only, not built | Partly |

Size: about 51k lines of Python in `miriam_agent/`, 81 test files (about 1,138 test functions), 114 commits between 2026-09-11 and 2026-09-28. The last recorded full run (from the audit doc) was 1270 passed, 17 skipped, 3 known failures. I did not re-run it.

### 2.3 What Miriam cannot do today (business view)

- **No tenancy.** No org, business, team or membership concept. Roles are only JWT claims (`user`, `verified`, `guest`).
- **One confirmer.** The confirm flow has one approver. Payroll needs two people (maker and checker).
- **Limits are per user, in NGN units.** Defaults are 2,000 auto, 5,000 per transaction, 10,000 per day. Business use needs per-org, per-role and per-payee limits.
- **No bulk operations.** No payroll batch, no multi-payee payment run.
- **No scheduler.** No cron, Celery or push in Python. Time-based work (payroll day, tax due date, invoice reminders) needs a new scheduler or reuse of Go's.
- **Durability risk.** The real ledger state is JSON in Redis. That is fine for a consumer prototype and not for a business system of record.
- **Money model inconsistencies.** `FinancialProfile` uses `Float`. Hands uses `Decimal`.
- **Regex routing is brittle for business language** ("run payroll for March", "pay invoice 4421"). It is auditable but will need a typed intent layer, not more regexes.
- **No database migrations.** Tables come from `Base.metadata.create_all`.
- **Blob files** over the 700-line architecture ratchet: `financial/intelligence.py` (3,280), `onboarding/service.py` (2,244), `api/chat.py` (1,693), and others. `test_blob_ratchet` already fails.

### 2.4 Blocking issue: merge conflict

`miriam_agent/config/settings.py` is in state `UU` (lines about 153 to 173). Python raises a `SyntaxError`, so nothing imports and tests cannot be collected. The two sides:
- Current branch adds agent budget settings (`AGENT_MAX_TOOL_ROUNDS=5`, `AGENT_MAX_TOKENS_PER_TURN=12000`, `AGENT_MAX_COST_USD_PER_TURN=0.05`, `AGENT_WALL_CLOCK_S=30`) and sets `TYPESAFE_REDACT_USER_TEXT` default to **False**.
- The incoming side sets `TYPESAFE_REDACT_USER_TEXT` default to **True** and has no budget settings.

This is a real decision, not a mechanical merge: keep the budget settings from one side, and pick the redaction default. For a business product handling payroll and bank data, I would default to **redact = True** (privacy first) and keep the budget settings. Confirm this before merging.

---

## 3. What exists: Rail (Go)

Numbers come from a read-only audit. I did not read `.env`, run the code or run the tests.

### 3.1 Size and shape

About 329k lines of Go in `internal/`, 1,015 non-test Go files, 242 test files (about 1,516 test functions), 75 service packages, 45 background workers, 21 provider adapters, 280 migrations (numbered up to 320), about 606 HTTP routes. Clean/hexagonal layering; hand-wired DI in a 5,000-line `internal/infrastructure/di/container.go`.

### 3.2 What is reusable

| Capability | Where | Maturity | Business use |
| --- | --- | --- | --- |
| Double-entry ledger, pending/commit/fail, reversal, hash chain, outbox, snapshots, velocity limits | `internal/domain/services/ledger/` | Production (consumer USD/USDC) | Pattern reusable. Schema is not (see 3.3) |
| Reconciliation and many recovery workers | `internal/domain/services/reconciliation/`, `internal/workers/*_recovery` | Working. Many one-off recovery scripts suggest real drift incidents | Reusable pattern |
| NGN virtual accounts (collections) | `adapters/graph`, `services/funding` | Working | Direct fit for receivables |
| Open banking (Nigeria, Ghana, Kenya, South Africa) | `adapters/mono`, `services/mono` | Working | Direct fit for cash visibility |
| Bank statement parsing (PDF/image to transactions to categories) | `services/statement/` | Working. NGN-centric. 2 test files | Direct fit for onboarding a business with no bank connection |
| Document OCR and field extraction | `services/document/`, `services/ocr` sidecar | Working or partial. Rule-based | Fit for invoice and receipt capture, needs work |
| Bill pay (airtime, data, cable, electricity) | `adapters/airbills`, `services/billpay` | Working | Minor fit |
| NGN on/off ramps | `adapters/paj`, `adapters/ramphub` | Working, fragile (many recovery workers) | Limited. Ramps are not bulk bank payouts |
| USD/EUR virtual accounts, fiat payouts | `adapters/bridge` | Working, legacy (mid-migration off it) | Possible for USD/EUR |
| KYC and AML | `services/kyc` (Sumsub, Didit, Bridge), `services/compliance` | Working. Individuals only, no KYB | Needs KYB |
| Confirmation cards (WebAuthn / Face ID) | `services/confirmation` | Working | Base for approvals |
| Automations and obligations (recurring payments) | `services/automation`, `services/obligation` | Working | Base for scheduled payables |
| Agent auth: short-lived agent JWT plus service key | `pkg/auth` `GenerateAgentToken`, `RequireRailServiceKey` | Working. User-scoped, not org-scoped | Reusable, must become org-scoped |
| Worker infrastructure: job queue, leader election, tickers | `pkg/jobqueue`, `internal/workers/` | Working. Each worker is a hand-written ticker | Reusable. Consider a real workflow engine later |

### 3.3 What blocks a business product

- **Ledger currency.** Migration `056_create_ledger_tables.up.sql` has `CHECK (currency IN ('USDC','USD'))` on accounts and entries. NGN, GBP and EUR live only at the edges. `funding/bridge_virtual_account.go` says outright that "the ledger is single-currency". A Nigerian business product needs NGN-native accounting.
- **Account types** are a constrained enum (altered in 8+ migrations) with consumer types (`stash`, `goal`, `card_hold`).
- **No tenancy.** Grep for `organization`, `kyb`, `maker.checker` returns nothing in Go or migrations. Users have a `role` (`user`, `admin`, `super_admin`). `household_groups` exists for families, not teams. `api_keys` exist but are per user.
- **Missing business domains:** invoices and receivables, payroll and bulk payouts, accounting sync (Xero, QuickBooks), expense management, corporate cards, multi-signer approvals, outbound webhooks to customers. `pkg/bulk` is a goroutine helper, not bulk payouts. GraphQL is effectively dead (one resolver).
- **No bulk NGN bank payout rail.** No Paystack, Flutterwave, Moniepoint, Wise, Stripe or Plaid adapters. This is the most important missing rail for payroll.
- **No GBP/UK Faster Payments or SEPA** beyond Bridge virtual accounts.

### 3.4 Tech debt and hygiene (fix before onboarding a team or an auditor)

- Root clutter: 13 tracked `*-task-def.json` files, a tracked 9 MB binary (`create_gas_wallet`), tracked `coverage.out` (5 MB), `dump.rdb`, a tracked `.env.staging`, `.backup/` dead code, and agent-config folders (`.claude`, `.codex`, `.cursor`, `.kiro`, and more).
- About 8,300 tracked files under `umbra-sidecar/node_modules`. The `.git` directory is about 730 MB.
- **Check `.env.staging` for real secrets and rotate anything that was ever committed.** I did not read it.
- Conflicting deploy targets in the repo (AtlasFlow per `AGENTS.md`, plus AWS ECS workflow, Fly, Kubernetes, Terraform, Cloudflare). Doc drift (`AGENTS.md` says Go 1.24, `CLAUDE.md` says 1.25, `README` still says Bridge).
- Single-file hubs: `di/container.go` (5k lines), `routes.go` (2.2k lines).
- Provider churn (Bridge to Circle, Reflect to Blend, Paj and RampHub) with one recovery worker per provider.
- Thin unit tests on money-critical packages (`ledger`, `funding`, `withdrawal`, `card`, `limits`, `billpay`). CI coverage floor is only 40%.
- `sanctions_screening.go` describes itself as a framework plus local fuzzy matching, and says to integrate a third-party API. Treat as partial.

### 3.5 The Python and Go relationship

- Go calls Python: `POST /api/v1/chat`, `/proactive/analyze`, `/money/inflow`, `/money/debit`, `/users/merge` (mints a per-user agent JWT plus `X-Rail-Service-Key`).
- Python calls Go: about 60 paths (balances, funding, bill pay, p2p, automations, obligations, vault, Glider investments).
- **Dual brain.** Go has its own Miriam orchestrator (`internal/domain/services/ai`, 33.7k LOC, and `services/miriam`, 7.5k LOC) that overlaps heavily with the Python agent. `platform_python_delegate.go` picks which one answers. For the pivot, choose one brain (I recommend Python) and stop investing in the other.
- Unused by Python but relevant for business: statements and documents endpoints, `/mono/*`, `/withdrawals`, `/deposits`, `/wallets`, `/limits`, cash-flow forecast, financial audit, tax summary, weekly report, money-guard.
- Header contract: Python sends `X-Miriam-Confirm-Id` and `X-Miriam-Receipt-Id` on Go mutations. Go enforces the confirm-id header on `POST /p2p/send` for agent tokens, but only when a flag is on, and it defaults to a no-op. I did not find a Go check for the receipt-id header. Turn enforcement on and check receipts before any business money movement.

---

## 4. What the market says

Confidence key: **High** = several independent or primary sources. **Medium** = one credible source. **Low** = vendor blog or AI-generated content, use as color only.

### 4.1 Global: agentic finance is here, and it converged on one design

| Player | What they ship | Design lesson for you |
| --- | --- | --- |
| **Ramp** (agents.ramp.com) | Agents get an identity, an owner, a budget, approved payment methods, and an audit trail. Human approver, merchant restrictions and receipts required | Copy the model: every agent has identity, owner, budget, policy, audit trail |
| **Airwallex** (agentic finance guide, July 2026) | T:0 AI-native ERP with four agents (Accountant, Scout, CFO, Integrator). Expense-policy agent reads a plain-English policy and checks every transaction. AP agent catches duplicates and changed vendor bank details. AgentOS: "no money-out by default" and a confirm flag | "No money-out by default" is a selling point for CFOs. Plain-English policy is a great UX |
| **Puzzle** (June 2026 comparison) | Continuous close on a schedule, up to 98% auto-categorization (vendor claim), reconciliation from about 2 hours to 5 minutes (vendor claim). Human approval before entries are final | Autonomy that runs without a login and human approval before anything is final |
| **Digits, Basis, Campfire, Rillet** | Autonomous-first bookkeeping (Digits), agents for accounting firms (Basis), AI-native ERPs for Series A/B+ (Campfire, Rillet) | Most autonomy lives where a finance team exists to catch errors. SMEs without a controller need approval gates |
| **Brex** | Reported as being acquired by Capital One (fintechlabs, April 2026) | Consolidation in the US spend market |

Market size figures conflict between sources ($12.4B "agentic AI in finance" per chatfin; $29.7B "autonomous finance" per an Airwallex-cited report). I would not quote either. Direction (growth, budgets shifting from copilots to agents) is consistent across sources. **Confidence: Medium.**

CFO sentiment (Salesforce survey via ITPro, in the Houseblend guide): conservative AI stance fell from 70% in 2020 to 4% in 2025. The top concerns are privacy and ethics risk (66%) and long ROI timelines (56%). Buyers want decision lineage and human sign-off. **Confidence: Medium.**

Also noted by several sources: many agent deployments fail on exceptions, data quality and trust. I could not open the Safebooks AP guide directly, so this is only a search-snippet claim. **Confidence: Low.**

### 4.2 Nigeria and Africa: the local picture

- **Scale.** Nearly 40 million MSMEs, about 48% of GDP; an estimated $158 billion annual MSME financing gap (MTN-SMEDAN, Leadership, May 2026). **Confidence: Medium.**
- **Compliance is a forcing function in 2026.**
  - Nigeria Tax Act 2025 took effect 1 January 2026. It rewrote PAYE brackets, removed CRA (consolidated relief allowance), introduced Rent Relief, made NHF voluntary, and renamed FIRS to NRS (source: AnooreHR, a payroll vendor). **Confidence: Low-Medium.** Verify every rate with an accountant before coding any tax logic.
  - E-invoicing: from January 2026, medium and small VAT-registered businesses must comply (VATupdate and GlobalVATCompliance headlines). I could not open the full articles. **Confidence: Low-Medium.**
  - CBN March 2026 AML directive: real-time monitoring, customer risk profiling, sanctions screening. AI monitoring is allowed but must be independently audited each year. Fintechs have 24 months to implement (Techpoint, an AI-assisted article). **Confidence: Low-Medium.**
  - Draft APP-fraud rules could make fintechs share liability for payments a user approved. This matters a lot for an agent that initiates payments.
  - NDPA 2023 and GAID 2025: data protection impact assessments, 72-hour breach notification.
- **Customer pain (consistent, plain, and it is your product):** profitable businesses die of cash shortfalls; customers pay late; statutory money (PAYE, VAT, WHT, pension) gets spent and then penalised; payroll runs on spreadsheets; business and personal money are mixed (AnooreHR cash-flow guide). **Confidence: Low** (vendor content), but matches every founder story I know of. Validate in interviews.
- **Competitors and adjacent players.**
  - Payroll: PayDay HR (about ₦1,500 to 3,000 per user per month), SeamlessHR (₦8,000 to 15,000, 50+ staff), AnooreHR (from free, about ₦3,000), BambooHR and Rippling via add-ons. Gusto does not operate in Nigeria. Price points are from a competitor's blog; **Confidence: Low.**
  - Banking and payments: Moniepoint (business banking, working capital, expense management, POS), Flutterwave, Paystack, Remita, Klasha. Moniepoint is the incumbent to respect: agent network, distribution, lending.
  - SME-OS startups: Zazu (South Africa and Morocco; invoicing, bookkeeping, cash-flow; $1M pre-seed, 1,000+ waitlist) shows investors fund this thesis. **Confidence: Medium.**
  - African funding is healthy but selective: $1.44B across 146 deals in H1 2026 (fewer, larger deals than 2025). **Confidence: Medium.**
- **Gap I believe exists (hypothesis):** nobody I found offers an always-on, WhatsApp-native agent that combines cash watch, payroll, and statutory-tax discipline for Nigerian SMEs. Incumbents are either banks (money, little intelligence) or HR/accounting tools (software, no proactivity). Validate before betting the company on it.

### 4.3 What the market implies for design

1. **Trust beats capability.** Agent identity, budget, policy, approver and audit trail are table stakes, not extras.
2. **Human-in-the-loop for money-out.** Every credible product does it.
3. **Explainability.** CFOs and regulators want to see why the agent said something. Your grounding guard and receipts are a real differentiator.
4. **Local compliance is the moat.** Global tools do not do PAYE, NHF, NSITF, ITF, WHT or NRS e-invoicing. A deterministic, dated, tested rule engine for Nigeria is worth more than a smarter chat model.
5. **Channel matters.** Nigerian SMEs live in WhatsApp. Chat-first, with a light dashboard for approvals and reports.

---

## 5. What is possible: feasibility map

Ratings are relative to what the two codebases give you today.

- **Ready**: mostly reuse. Days to a few weeks.
- **Moderate**: reuse plus real new build. Weeks.
- **Hard**: new domain, new tables, new workflows. Months.
- **Partner/Licence**: needs a licensed third party or regulatory work first.

| Capability | What the agent does | Feasibility | Why |
| --- | --- | --- | --- |
| Cash position and runway | Shows balances across linked accounts, forecasts 4 to 12 weeks, alerts on projected shortfall | **Ready to Moderate** | Mono linking, statement parsing, cash-flow forecast endpoints and proactive analyst exist |
| Statement and receipt ingestion | Accepts PDF, photo or CSV, categorizes, flags oddities | **Ready** | Go statement and document pipelines exist |
| Anomaly and duplicate detection | Flags duplicate payments, unusual amounts, new recurring charges, changed vendor bank details | **Moderate** | Go anomaly engine and obligation detector exist. Needs payee model |
| Statutory tax calendar and ring-fencing | Tracks PAYE, VAT, WHT, pension due dates, tells you how much to set aside | **Moderate** | Needs a new deterministic Nigerian tax module (dated rules) |
| Payroll preparation | Builds the monthly payroll, computes PAYE and deductions, produces payslips, journal entries and a bank upload file | **Moderate to Hard** | Needs employee model, tax engine, payslips, approvals |
| Payroll payout | Pays staff from the business account | **Hard + Partner** | Needs bulk NGN payout rail, maker-checker, limits. Likely needs a licensed partner |
| Receivables and collections | Issues invoices, sends reminders, chases late payers, matches incoming payments | **Moderate** | Graph virtual accounts help matching. Needs invoice model and messaging |
| Payables | Captures bills, matches to payees, schedules and pays after approval | **Hard** | Needs payee, bill, approval and bulk payout |
| Bookkeeping and monthly close | Categorizes, reconciles, drafts journals, produces P&L and cash flow | **Hard** | Needs a business chart of accounts and NGN ledger |
| Accounting sync | Pushes to Xero, QuickBooks, Zoho | **Moderate** | Standard APIs, but a new integration each |
| Owner "CFO" briefings | Daily and weekly WhatsApp summary: cash, what is due, what needs a decision | **Ready** | Proactive analyst plus voice, with new prompts |
| Plain-English policy checks | "No spend above ₦200k without approval" enforced on every payment | **Moderate** | Hands `Policy` exists. Needs org scoping and a policy language |
| Multi-approver workflows | Two-person approval on payroll and large payments | **Moderate** | Confirmation cards exist. Needs N-approver model and roles |
| Spend cards and expenses | Employee cards, receipt matching | **Hard + Partner** | Requires a card-issuing partner and KYB |
| Working-capital offers | Recommends short-term finance based on cash flow | **Partner** | Regulated. Start with referrals |
| FX and multi-currency treasury | Holds and converts NGN, USD, GBP | **Hard + Partner** | Ledger is USD/USDC only. Rates from ramp adapters |
| Business KYB | Verifies the company, directors, CAC (Corporate Affairs Commission) registration | **Moderate + Partner** | Sumsub and Didit offer KYB products. Not wired |
| Large-enterprise features | SSO, ERP connectors, entity consolidation, SOC 2 style controls | **Hard** | Out of scope for MVP |

### 5.1 Customer segments

| Segment | Fit | Notes |
| --- | --- | --- |
| **SMEs (10 to 100 staff)** | Best first target | Payroll and cash pain is sharp, no finance team, buys on WhatsApp. Need approvals to be light |
| **Payroll and accounting bureaus** | Strong second target and a channel | One bureau brings 20 to 200 clients. Forces you to build multi-client tenancy well. Similar to how Basis sells to accounting firms |
| **Micro businesses (1 to 10 staff)** | Volume, low revenue | Free tier and referral funnel. Do not optimize for them first |
| **Larger firms** | Later | Long sales cycles, need ERP and SSO. The safety architecture is a good fit, but the surrounding product is a lot of work |

---

## 6. Recommended architecture for the pivot

### 6.1 Principles (keep what works)

1. **Separation of powers stays.** Deterministic code moves money. Typed judgment decides. The model narrates and reads. The LLM never calculates money or tax.
2. **The agent proposes, humans approve.** Money-out needs approval by the right role. Read and draft actions are automatic.
3. **Every agent action has an identity, owner, budget, policy and audit record** (the Ramp model).
4. **Go stays the money authority.** Python is the brain. Retire the Go-native Miriam brain over time.
5. **Deterministic, dated, tested tax rules.** Effective-dated rule packs, reviewed by a Nigerian accountant, with hashes stored on every calculation for audit.

### 6.2 New building blocks

```
                 ┌──────────────── Channels ────────────────┐
                 │ WhatsApp   Email   Web app (approvals)   │
                 └───────────────────┬──────────────────────┘
                                     ▼
  ┌────────────────────────── Miriam (Python) ──────────────────────────┐
  │ Intent layer (typed)  →  Agent loop (read-only)  →  Voice + guards   │
  │ Watchers (deterministic detectors, scheduled + event-driven)         │
  │ Proposals (draft payroll, draft reminder, draft payment batch)       │
  │ Policy engine (org rules)     Approvals (roles, N approvers)         │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼ (agent token scoped to org + role)
  ┌────────────────────────── Rail (Go) ────────────────────────────────┐
  │ Business domain (new): orgs, members, roles, KYB, bank accounts,     │
  │   transactions, payees, employees, invoices, bills, payroll runs,    │
  │   tax obligations, approvals, policies                               │
  │ Existing: Mono, Graph, statements, documents, confirmations, workers │
  │ Later: payout partner adapter (bulk NGN), accounting sync            │
  └──────────────────────────────────────────────────────────────────────┘
```

### 6.3 Concrete decisions to make

| Decision | Recommendation | Why |
| --- | --- | --- |
| Where does the business data live? | New tables in Rail with an `org_id` on everything, NGN-native `NUMERIC(20,2)` plus currency. Do not extend the consumer ledger yet | The consumer ledger has USD/USDC constraints and consumer account types. Retrofitting risks live user funds |
| Tenancy model | `organizations`, `memberships(role)`, roles: owner, finance admin, approver, viewer, agent. Add a `bureau` parent later | Bureaus need a parent-child model. Cheaper to design now than migrate later |
| Agent auth | Agent token carries `org_id`, `role=agent` and a scoped budget. Enforce on every Go route | Today agent tokens are user-scoped |
| Scheduler | Reuse Go workers and leader election for the tick. Add a `watchers` package in Python that Go calls (extends the current `proactive/analyze` pull contract) | Least new infrastructure. Revisit a workflow engine (Temporal or similar) only if it hurts |
| Channel | WhatsApp Business API first, email second, minimal web for approvals | Nigerian SME behavior. The iMessage gateway pattern (`apps/spectrum-gateway`) is a template |
| Bulk payouts | Phase 2, via a licensed partner (for example Paystack Transfers, Flutterwave, or a bank API), not your own licence | Avoids CBN licensing at the start. Verify partner terms and fees before committing |
| Persistence of Python ledger | Move any business state to Postgres. Keep Redis for cache and locks only | Durability |
| LLM keys and data | Default `TYPESAFE_REDACT_USER_TEXT=True`. Redact account numbers and employee PII before any model call | NDPA and trust |

### 6.4 What to freeze or remove

Feature-flag off, then delete after the pivot is validated: onboarding interview and plan builder, household envelopes, 70/30 split and standing rules, Glider investing and vault, Alpaca, copy trading, roundups, gameplay and goals, travel booking, Umbra sidecar, public trades, iMessage persona prompts. This reduces the surface you have to secure and explain.

---

## 7. MVP: "Finance Watchtower + Payroll Co-pilot"

### 7.1 One-sentence pitch

"A finance agent that watches your business's money around the clock, tells you what needs attention on WhatsApp, and prepares payroll and payments for you to approve, so you never miss salaries, tax dates or late payers."

### 7.2 Scope (what is in)

1. **Business onboarding.** Sign up, create the org, invite an approver. Business verification can be manual for design partners. Real KYB comes with money movement.
2. **Connect the money.** Mono account linking, statement upload (PDF/photo/CSV) and manual entries. Go pipelines already exist.
3. **Watchers** (the "always-on" core). Deterministic detectors, run on a schedule and on new transactions:
   - Low runway or projected shortfall before payday.
   - Payroll due in N days and cash is short.
   - Statutory remittance due (PAYE, VAT, WHT, pension) with the amount to set aside.
   - Overdue receivable with a drafted reminder.
   - Duplicate or unusual payment, new recurring charge.
   - Large or off-hours movement.
4. **Payroll co-pilot.** Employee list, monthly payroll draft, PAYE and deductions from the deterministic tax module, payslips, a bank bulk-upload file, and a journal-entry summary. Two-person approval. **No payout in MVP.**
5. **Daily and weekly briefing** on WhatsApp: cash, what is due, what needs a decision.
6. **Approvals.** Approve or reject drafts in chat or on a simple web page, with a role check and an audit record.
7. **Audit and explainability.** Every message and proposal links to the data behind it. Reuse the grounding guard and receipts.
8. **Evals.** A business eval suite (see 7.5) gated in CI.

### 7.3 Explicitly out of scope for MVP

Payout execution, corporate cards, lending, FX treasury, full bookkeeping and close, accounting sync, enterprise features, multi-country, and the investing and savings products.

### 7.4 Success measures (set targets with design partners)

- 5 to 10 design-partner businesses actively using it weekly.
- Time from signup to first useful alert under 24 hours.
- At least one "saved me" moment per business per month (a caught shortfall, missed tax date, or duplicate payment). Ask for it.
- Zero incorrect tax or payroll figures in the eval suite and in accountant spot checks.
- Alert precision (share of alerts users find useful) tracked from day one.
- Willingness to pay: a paid pilot from at least three design partners.

### 7.5 Trust and quality gates

- Payroll and tax outputs must match a Nigerian accountant's calculations on a fixed set of test cases before any customer sees them.
- Extend the hallucination eval (`eval/hallucination_cases.py`) with business cases: wrong payee, invented invoice, wrong tax figure, stale rates.
- No LLM-generated numbers. Figures come from deterministic modules and are clamped by the voice layer.
- Per-tick cost cap and per-org budget on LLM use (the settings from the merge conflict already model this).

---

## 8. Roadmap (rough, assuming 1 to 2 engineers; treat as estimates)

### Phase 0: Unblock (about 1 week)
- Resolve the `settings.py` merge conflict, get `uv run pytest` green, and check `git log` for what `807fb97` brought.
- Decide the go-to-market wedge, the first city and industries, and design partners.
- Rotate any secrets that were ever committed (check `.env.staging`), and remove tracked binaries, `node_modules` and task-def clutter from Rail.
- Decide: Python is the single brain. Freeze Go-native Miriam work.

### Phase 1: Foundations (about 3 to 5 weeks)
- Rail: organizations, memberships, roles, org-scoped agent token, audit fields with `org_id`. Business tables (accounts, transactions, payees, employees, invoices, obligations).
- Python: org-aware context, per-org policy and limits, N-approver confirm flow, Postgres migrations (Alembic), business persona and prompts, typed intent layer for business commands.
- Turn on Go-side enforcement of confirm-id and receipt-id headers.
- Feature-flag off consumer features.

### Phase 2: The always-on core (about 3 to 4 weeks)
- Watchers framework plus the first five watchers.
- WhatsApp channel and daily briefing.
- Connect Mono plus statement ingestion for business accounts.
- Approvals UX (chat and a minimal web page).

### Phase 3: Payroll co-pilot (about 4 to 6 weeks)
- Deterministic Nigerian tax module (PAYE, pension, NHF, NSITF, ITF, WHT), effective-dated, reviewed by an accountant.
- Payroll run, payslips, bank upload file, journal summary, two-person approval.
- Receivables: invoice creation, reminders, matching via Graph virtual accounts.

### Phase 4: Design-partner pilots (parallel from Phase 2)
- Run with 5 to 10 businesses. Weekly calls. Measure the success metrics above.

### Phase 5 (after validation): Execution and scale
- Bulk payout via a licensed partner, KYB, maker-checker on payouts.
- Bureau (multi-client) mode.
- Accounting sync. Then payables and cards.

Do not commit dates to Sir Nzube until the Phase 0 decisions and the team size are known.

---

## 9. What to do first (next 10 actions)

1. Ask Sir Nzube the questions in section 10 (geography, custody stance, team size, budget, design partners).
2. Resolve the merge conflict in `miriam_agent/config/settings.py`. My recommendation: keep the budget settings and set `TYPESAFE_REDACT_USER_TEXT` default to True.
3. Run `uv run pytest tests/ -x` and record the true baseline. Fix or quarantine the 3 known failures.
4. In Rail, inspect `.env.staging` and git history for secrets, rotate anything exposed, and clean tracked binaries and `node_modules`.
5. Interview 8 to 10 Nigerian business owners or payroll and accounting people. Ask about the last time they nearly missed payroll or a tax date. Confirm the wedge before building it.
6. Write the tenancy and roles design (one page) and review it with the Go side. This is the riskiest schema change.
7. Prototype the first watcher end to end (low runway before payday) using existing Mono, statement and proactive-analyst pieces. It proves the always-on loop in days, not months.
8. Get a Nigerian accountant to agree on a set of payroll and tax test cases and expected outputs. This becomes the golden eval.
9. Get legal advice on: what Rail's current custody and licensing position allows for business accounts, and the partner route for payouts.
10. Choose the WhatsApp provider (Meta Cloud API directly or a BSP) and start the business verification process. Approval can take time.

---

## 10. Open questions and what I could not verify

### Questions for you and Sir Nzube
- Nigeria only at first, or Nigeria plus UK/US from day one? (Rail supports GBP/USD/EUR only at the edges.)
- Do we hold customer funds, or orchestrate payments through partners? This decides licensing.
- What is Rail's current regulatory position for consumer funds, and does it extend to businesses?
- How many engineers, and what runway, for this pivot?
- Are there design-partner businesses Sir Nzube can introduce?
- Pricing hypothesis: per seat, per employee (payroll), or a percentage of volume?
- What happens to existing consumer users of Miriam and Rail? Sunset, keep, or separate?

### Not verified in this analysis
- I did not run either test suite or the code. Maturity ratings come from structure, wiring and docs.
- Go-side behavior was inferred from a read-only audit. Specifically unverified: which currencies Bridge virtual accounts actually return, whether `/external/webhooks` is implemented, whether `X-Miriam-Receipt-Id` is enforced anywhere in Go, and which KYC provider path is live (Sumsub is wired outside the DI package).
- Some audit claims in `docs/MIRIAM-AUDIT.md` are stale. For example, it credits a `safety/confirmations.py` file that no longer exists, and the blob-ratchet test now exists and fails.
- Market data mixes primary sources with vendor blogs and AI-assisted articles. I could not open two pages (the NRS e-invoicing articles and Safebooks AP guide). All Nigerian tax rates, e-invoicing thresholds, CBN deadlines and competitor prices need confirmation from primary sources (NRS, CBN, vendor pricing pages) and an accountant or lawyer before you rely on them.
- I did not research the UK or US business market in depth beyond the global agentic-finance players.
- I did not find a dominant AI-native finance agent for Nigerian SMEs. Absence in my search is not proof. Ask customers what they use today.

---

## Appendix A: Sources

Primary or company pages: Ramp agents (agents.ramp.com), Airwallex agentic finance guide (airwallex.com, 2026-07-13), Puzzle "AI agents for finance" (puzzle.io, 2026-06-24), Moniepoint business page and Wikipedia entry, CBN payment service providers page.

News and analyst: Leadership (MTN-SMEDAN, 2026-06-01), Portal ERP (Zazu, 2026-07-23), Techpoint (CBN fintech regulations, 2026-05-05, AI-assisted), Houseblend CFO guide (2026-02-20, relies on ITPro, TechRadar and Axios summaries), FintechLabs (Mercury vs Brex vs Ramp, 2026-04-16), African startup funding roundups (H1 2026).

Vendor blogs (low confidence): AnooreHR payroll comparison and cash-flow guide, ChatFin and Sana Labs finance-agent roundups.

Search results seen but not opened: VATupdate and GlobalVATCompliance Nigeria e-invoicing, BDO Nigeria VAT under the NTA, SMEDAN and NBS MSME survey, Safebooks AP guide.

## Appendix B: Repo pointers

- Miriam: `STATUS.md`, `ONBOARDING_ARCHITECTURE_MAP.md`, `docs/ARCHITECTURE-CONTRACT.md`, `docs/MIRIAM-AUDIT.md`, `miriam_agent/orchestrator.py`, `miriam_agent/hands/`, `miriam_agent/judgment/`, `miriam_agent/proactive/`, `miriam_agent/integrations/go_client.py`, `eval/hallucination_cases.py`.
- Rail: `CLAUDE.md`, `AGENTS.md`, `internal/domain/services/ledger/`, `internal/domain/services/statement/`, `internal/domain/services/mono/`, `internal/domain/services/confirmation/`, `internal/infrastructure/ai/python_agent_client.go`, `internal/api/middleware/middleware.go` (confirm-id check near lines 506 to 541), `migrations/056_create_ledger_tables.up.sql`.
