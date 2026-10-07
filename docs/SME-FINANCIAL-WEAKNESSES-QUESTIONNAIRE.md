# SME Financial Weaknesses Questionnaire

**Goal** of this doc: translate the three things we must learn — (1) their financial weaknesses, (2) how they manage/minimize finance work, (3) how we can help — into questions that each map to a verified system-design risk from `MIRIAM-SYSTEM-DESIGN-ARCHITECTURE.md` and `MIRIAM-AUDIT.md`.

Every question is tagged with the gap it resolves. If an answer doesn't land in one of these buckets, it's noise.

---

## Section 1 — Financial Weaknesses (what breaks, and how)

These find the cracks where *invented balances, fail-open audits, and Float-vs-Numeric* leaks actually happen in the wild.

| Q# | Question | Why we ask (design-doc gap it resolves) |
|----|----------|-----------------------------------------|
| 1.1 | In the last 3 months, tell us the last time you were *sure* you had money in your account, but the bank said it wasn't there (transfer delayed, POS settlement stuck, cheque bounced). What happened and how long did it last? | Surfaces the **delayed-settlement** gap (design doc G27 ingestion; TRD §16 "capture-QA/quarantine tier"). This is the exact scenario the quarantine ledger column exists for — money that looks real but isn't cleared yet. |
| 1.2 | When payroll or tax money "meant" for a due date got used for something else instead — what pulled it away? Be specific: was it a supplier you couldn't refuse, an emergency, or did you not notice until it was too late? | Resolves **money-diversion** (G22 two-process money minting / no single-ledger discipline). The answer tells us whether the fix is a hard budget cap (§C1 limits) or a hard notification gate (§C5 single-ledger rule). |
| 1.3 | Which business expense scared you the most in the last 6 months — the biggest one you weren't sure you could cover on the due date? | Maps to **confidence-interval gaps** in the proactive reacher (proactive/analyst.py fail-open). We need at least one near-miss example to calibrate when `Conflicted` vs `Unverified` tier alerts fire. |
| 1.4 | Have you ever had two different numbers for "how much is in the account" — your book vs your bank vs your accountant — at the same moment? Which one was right and how did you settle it? | This is the **Float-vs-Numeric / no golden reconciliation** risk (C1). If they can't state which source is authoritative, our ledger's `Verified/Reported/Unverified/Conflicted` tiers need a source-order rule before we ship. |

---

## Section 2 — How they minimize finance action (time, not money)

These answer the *avoidance* behaviour the WhatsApp-alert question half-caught. We're hunting the specific workaround that our automation must replace — or we'll build a bridge to nothing.

| Q# | Question | Why we ask (design-doc gap it resolves) |
|----|----------|-----------------------------------------|
| 2.1 | Walk us through a typical day when you have to check if money arrived or if a bill is due. What apps/accounts do you open, in what order, and where do you get bored/stuck? | Resolves **channel-priority deadlock** (design doc finding: "two bridges, neither WA-complete"). The order they open apps = the channel our alert must win. If they open Mono first and WhatsApp never, no amount of WA templating fixes it. |
| 2.2 | What's the smallest finance-related thing you do *every single day* that you'd happily pay someone else to do automatically? | This is the **granularity of "minimize"** test. If they say "check if yesterday's sales deposited," that's a send_gate + dedupe key (`req_org_shop_msg_job_v1`). If they say "figure out how much to move between accounts," that's a trust-rail (§C3) we haven't proven we can earn yet. |
| 2.3 | When you're busy and money stuff piles up, what's the *first* thing you ignore — and what's the *last* thing you ignore (the one that will get you fired if missed)? | Maps to **alert-priority / calm-down window** (`PROACTIVE_MIN_INTERVAL_HOURS=12` in proactive/state.py). The "last thing ignored" is the P0 trigger; the "first thing ignored" tells us what noise to suppress so we get the 12h calm-down without losing a real crisis. |
| 2.4 | Do you use any tool today that *already* reminds you about money — even a spreadsheet, a calendar note, or a WhatsApp broadcast list? What does it get right and where does it fail you? | This kills the **pretend-we're-different** trap. If they're using a WhatsApp broadcast, our iMessage/WA dual-channel strategy (§2.2) must converge on broadcast-style templating, not conversational agents. If a spreadsheet, the fix is structured intake (G27), not more chat. |

---

## Section 3 — How we can help (the actual ask, not our assumption)

These force a *constraint* on the solution shape — which is what the avoid-list and milestone gate exist to protect us from over-building.

| Q# | Question | Why we ask (design-doc gap it resolves) |
|----|----------|-----------------------------------------|
| 3.1 | If we built something that could text you *before* a cash problem became a crisis, what's the minimum bar for you to act on it *today* — and what's the thing that would make you ignore it completely? | This is the **fail-open vs fail-closed** decision (audit finding: proactive/analyst.py is fail-open, dead backend = stay quiet). The "minimum bar" is the calm-down threshold; the "ignore it completely" is the false-positive ceiling that, if blown, breaks trust permanently. |
| 3.2 | What would you need to *trust* a number we tell you — proof, source, format, or who it comes from? | This is the **dual-channel verify + golden-Doc-200** test (TRD §16 T1-T11, specifically T5 dual-channel verify and the 200-sample golden set). If they say "I need to see my bank app open," we ship camera bank-only (G26) first; if they say "a call from my accountant," we build the referer/confirm-card flow (hands/settle_cards.py) first. |
| 3.3 | We can help with three things: (a) warn before money runs out, (b) keep tax/payroll money in its own lane, or (c) make it easy to record every payment without typing it. Pick one, rank the other three, and tell us why your top choice matters most. | This is the **Milestone 0 triage** (design doc §12 Milestones 0-4). It forces the SME to order our value props so we don't ship the Glider sleeve path (M3) before proving intake (M2) and money spine (M1). If (c) wins, the OCR sidecar (§7/F) ships before the proactive analyst. |
| 3.4 | What's the cheapest thing we could do that would still feel worth paying for — and what's the expensive thing you'd refuse to pay for even if it worked perfectly? | Final **willingness-to-pay calibration** against `AGENT_MAX_COST_USD_PER_TURN=0.05` and the proactive 700-token cap. If their floor is below 5¢/turn, we must batch turns (proactive analyze consolidation). If their ceiling is below any per-message WhatsApp cost (`$0.0067` per design doc §5 WA pricing), the delivery window math breaks. |

---

## How to use this

- **Each "why" maps to a file or gap** you can hand the engineer. If an answer contradicts a gap assumption, flag the gap for re-scoping, not feature-creep.
- **If answers to 2.1 and 2.2 diverge** (they open Mono first but want WA alerts), the bridge-priority finding deepens — we don't pick channels from our convenience.
- **If 3.1's "ignore it completely" overlaps 1.4's "two numbers"** we have proof the audit-tiering is needed before the proactive reacher ships (G22 order: money spine → audit → proactive).
