# Miriam SME Pivot — Product Requirements Document (PRD)

**Date:** 3 Oct 2026 · **Status:** Draft for approval · **MVP target:** 11 Oct 2026
**Linear project:** Miriam SME Pivot — MVP (11 Oct) · **Owner:** Tobiloba
**Channel decision (3 Oct):** iMessage-first for v1 — Meta business approval is blocked, so WhatsApp is deferred post-MVP. iMessage already works two ways (Mac line + Photon cloud) and carries the v1 reminder.
**Fair onchain touch (3 Oct):** World's Fair submissions close 12 Oct and judge onchain product. Cheapest touch that counts: every `matched` verdict writes one Solana devnet memo-anchor (`invoice_ref + match_id`) with an explorer link. No wallet, no mainnet, no funds in v1.
**Companions:** `MIRIAM-MASTER-GUIDE-SIMPLE.md` (plain-English front door) · `MIRIAM-WORKFLOW-SPECS.md` (3 workflow specs) · TRD + System Design (engineer depth)

---

## 1. Who is the first customer?

**The paying customer is the distributor/wholesaler** — one provisions distributor running a tight weekly route across **Oshodi + Mushin** markets in Lagos.

**The daily user is the retailer** — the market woman/man on that route who buys stock on credit or cash and pays by bank transfer to the shop's static account.

Why the distributor pays: they currently cannot see, per retailer, which standing invoices got paid, which are still open, and which need a human to chase. That blindness is what kills restock-on-credit. Miriam sells them that sight: a live reconciliation view per shop (matched / unpaid / needs-human-review) plus one automated nudge (payment reminder over iMessage in v1) that keeps money moving without extra staff.

The pilot story in Linear (MVP-6) models exactly this: 1 distributor org, 5 shops (3 Oshodi, 2 Mushin), standing invoices, inbound transfer alerts.

---

## 2. What specific financial problem are we solving?

**Payment reconciliation: which retailer payments match which standing invoices.**

Concretely, for every invoice the distributor issues (INV-OSH-0001, ₦84,500, due Friday) and every transfer alert that lands in a shop account, Miriam must resolve each pair into exactly one bucket:

1. **Matched** — payment and invoice agree (exact amount ± tolerance, right window). Money is accounted for; restock can continue.
2. **Unpaid** — invoice open past due with no matching payment. Needs a reminder, then a human.
3. **Needs human investigation** — amount mismatch, orphan payment with no invoice, stale unmatched payment >48h, suspected duplicate. A person must look; the agent must never guess.

If customer research confirms this as the initial problem (it is the assumption this MVP tests), the distributor outcome is: **open the reconciliation view and see, per retailer, what matches, what is unpaid, and what needs a human — with every naira traceable to a webhook receipt.**

---

## 3. How does the customer currently solve it?

Today, in Oshodi/Mushin provisions trade:

- The distributor's rep keeps **paper or a spreadsheet** of who took goods and who paid, updated when they physically visit or when a retailer forwards a transfer screenshot on WhatsApp/iMessage.
- Retailers **retype or screenshot** bank alerts ("I have sent it, see") — screenshots can be edited, amounts mistyped, sender names ambiguous.
- Matching is done **in someone's head**: "that ₦84,500 on Friday — was that Mama Nkechi's Peak milk invoice or Ahmed's rice balance?" Part-payments, shared names, and delayed settlement make this error-prone.
- Unpaid invoices surface **only at restock time** ("you still owe last week, no new goods") — confrontational, late, and blind to dispute-vs-default.
- There is **no audit trail**: when money is disputed, there is no receipt chain from webhook → match → reminder to point at.

Miriam replaces the head-matching and the screenshot-trust with webhook truth + deterministic matching + a receipt for every decision.

---

## 4. What should Miriam do differently?

Three complete workflows, not disconnected features (full specs in `MIRIAM-WORKFLOW-SPECS.md`):

1. **Financial data ingestion (WF-1):** distributor invoice CSVs + partner/Mono transfer webhooks land through one stamped front door. Signature-verified, replay-safe, sub-second. Pasted chat text can never become a payment row — the fake-alert guard refuses it with "forward the bank alert, don't retype it."
2. **Financial monitoring and alert (WF-2):** deterministic code matches every payment to invoices (exact → fuzzy → no-fit → amount-mismatch paths), flips rows to matched/unpaid/needs_review, writes a best-effort Solana devnet memo-anchor per `matched` row (explorer link in the view), and surfaces the three-bucket distributor view. AI explains; code decides.
3. **One output action — payment reminder over iMessage (WF-3):** an overdue watcher (plain code, no AI) emits candidates with pre-added figures; the analyst (AI) renders exactly one reminder message from those figures; `send_gate` enforces the iMessage pacer + per-shop daily cap + quiet-hours rules (no templates, no 24h-window logic in v1 — those are WhatsApp concepts and return with the WhatsApp build); the outbox writes the receipt and the existing iMessage bridge delivers it.

The non-negotiable differences from today: **only webhook truth confirms money** (never screenshots), **amounts are kobo-exact end-to-end** (AI never computes), **every send is rate-gated** (pacer + daily cap + quiet hours), **every decision has a receipt** (match rows, outbox rows).

---

## 5. What does success look like?

**MVP exit bar (11 Oct — Linear MVP-8):**

- [ ] Seeded distributor view loads matched / unpaid / needs_review in <2s (matched rows show anchor_tx + explorer link).
- [ ] A new exact-amount test payment resolves to `matched` within 60s of webhook; ≥1 matched row has a CONFIRMED devnet memo tx.
- [ ] A wrong-amount test payment resolves to `needs_review/AMOUNT_MISMATCH`.
- [ ] An overdue invoice produces a real iMessage reminder on a test handset, live only (screenshot + provider_msg_id attached to MVP-8; no fallback footage).
- [ ] 20x webhook replay changes no counts (idempotency proof).
- [ ] CI merchant gates green (exact-kobo, fake-reject, replay, tenant, no-demo-mint; anchor RPC mocked in CI).
- [ ] Total demo send volume within the existing iMessage pacer (5,000/day); $0 template spend (no templates in v1).
- [ ] Fair submission pack: pitch video ≤2 min + demo video ≤3 min + repo link + explorer sig (portal due 12 Oct).

**Post-MVP signals (weeks after the 11th, to confirm the wedge before scaling):**

- ≥60% of pilot shops with ≥1 matched payment/week (the loop is alive).
- Match rate ≥85% auto-matched (exact + fuzzy) without human touch.
- `needs_review` cleared by a human within 48h (the queue doesn't rot).
- ≥1 distributor quote: "I can see who paid without calling my rep" (the paid-view value prop in their words).
- Per-shop weekly infra cost ≤ ₦2,900 (inside the Part-13 cost card).

---

## 6. What is explicitly outside version 1?

- **Camera till / OCR / vision** — no book photos, no Document AI, no VLM reading in v1. Intake is CSV + webhooks only.
- **Stock tracking, stock-out advice, price books** — no inventory tables in v1 beyond what reconciliation needs.
- **Loan packs, credit scoring, lender referral, wholesaler credit** — no underwriting, no first-loss, no disbursement. Reconciliation evidence now; credit later.
- **Fake-transfer point-of-sale confirm** ("give him the goods" YES/NO) — the grandma-explained flow is real but v2; v1 stops at distributor-side matching + reminders.
- **Multi-distributor scale, multi-market rollout** — one distributor, Oshodi + Mushin only.
- **WhatsApp (Meta blocked), USSD, voice notes** — iMessage only in v1; WhatsApp returns post-MVP once business approval lands (templates, 24h-window clock, per-message cost rows come with it).
- **Uplift-share pricing, outcome billing** — v1 is proved-loop + cost card, not monetisation mechanics.

Anything in this list appearing in a v1 issue or demo is scope creep — file it post-MVP.

---

## Appendix: traceability

| PRD section | Workflow spec | Linear issues |
|---|---|---|
| Ingestion (§4.1) | WF-1 | MVP-1 (tables), MVP-2 (front door) |
| Monitoring + distributor view (§2, §4.2, §5) | WF-2 | MVP-1 (tables), MVP-3 (matcher), MVP-6 (seed) |
| Payment reminder (§4.3) | WF-3 | MVP-4 (watcher/analyst/gate), MVP-5 (bridge) |
| Exit bar (§5) | all | MVP-7 (gates), MVP-8 (demo) |
