# P0 Plan (foundation)

**Date:** 11 Oct 2026 · **Owner:** Tobiloba · **Companions:** `MIRIAM-NETWORK-ARCHITECTURE.md`
(canonical) · `eval/capabilities.json` (gate catalog).
**Rule:** P0 ships behind the capability gate. No distributor flow is called "verified" until the
payment dedupe ticket is done.

Tickets are in build order. Each has acceptance criteria and the tests that prove it. Where a test
file is named, it is either an existing file to extend or a new file to create.

---

## P0-1. Confirm the reported CI failures (verification first)

**Why:** The brief reports 3 failing tests; I could not run the suite in this environment
(`timeout` unavailable, `uv run pytest` exceeded the tool budget). This ticket proves the real state
before anything is built on top.

**Do:** Run the three named tests and record pass or fail with output.

```
pytest tests/architecture/test_module_rules.py -q
pytest tests/test_money_layers_route_live.py::test_a_failing_audit_sink_never_breaks_a_settled_turn -q
pytest tests/test_trace.py::test_service_records_conductor_and_present_traces -q
```

**Acceptance criteria:** A written result for each test: green or red, with the failing assertion
quoted verbatim.
**Tests that prove it:** the three commands above.
**Blocks:** P0-2 (do not spend on a fix until the failure is reproduced).

---

## P0-2. Fix the CI failures, and rewrite the audit test for the split policy

**Why:** The gate cannot block deploys while main is red. Q1 is now answered, so the audit test is no
longer a policy question, only a rewrite. The network doc §9.4 and the always-on doc §4 split the
policy by sink: the durable money audit row is fail-closed in the settlement transaction, and
secondary sinks are fail-open via the outbox.

**Do:**
1. Fix `test_blob_ratchet` by registering or splitting whatever grew past its recorded size.
2. Fix `test_service_records_conductor_and_present_traces`.
3. **Rewrite `tests/test_money_layers_route_live.py:448`**
   (`test_a_failing_audit_sink_never_breaks_a_settled_turn`) to assert the split: a broken durable
   store means no receipt and a retry (nothing settles), while a broken secondary sink (OTel,
   Langfuse, analytics) still settles.

**Acceptance criteria:** `pytest tests/architecture tests/test_trace.py tests/test_money_layers_route_live.py -q`
is green, and two tests pin the split: one for the durable store (no receipt, retry) and one for a
secondary sink (settles).
**Tests that prove it:** the three files above, plus the two split tests.
**Decision needed:** none (Q1 answered).

---

## P0-3. Recover the capability gate from eval/capability-gate, merge the new entries

**Why:** The gate and the 124-entry catalog are on branch `eval/capability-gate` (session
`70286b98`, Q3), not in this checkout. The gate cannot block anything until it is applied here.

**Do:**
1. Recover the gate from `eval/capability-gate` and apply it with `git am`.
2. **Merge** the 15 new entries in the working `eval/capabilities.json` into the branch's 124-entry
   file. **Do not overwrite it.** Dedupe by `id`, and on a conflict keep the branch's entry.
3. Wire the gate into CI so trust areas must pass 100 percent and quality areas 90 percent, and it
   **blocks** the merge when red.

**Acceptance criteria:** A deliberately broken trust-area capability fails CI and blocks the merge.
The merged catalog loads and reports counts by area, with no duplicate ids.
**Tests that prove it:** a `ci`-level test that injects a failing capability and expects a non-zero
exit; a catalog-parse test asserting every entry has id, area, trust, scenario, pass_rule, target,
status, and that ids are unique.
**Depends on:** P0-2 (gate cannot block while main is red).

---

## P0-4. Real payment dedupe (table exists, work is not done)

**Why:** The table exists but is unfinished. `PaymentReference` on `m0/stop-the-bleeding` keys on
`(channel, sender_handle, amount_minor, reference, payload_hash)`, so two wordings of one transfer
become two rows, and production still dedupes on `inflow_id_for_alert` text hashes instead of the
table (**Q2**: the table is not written by any flow). Nothing may be called a verified payment until
this ships.

**Do:**
1. Add a migration that makes the unique key **exactly**
   `(channel, sender_handle, amount_minor, reference)`, keeping `payload_hash` as a plain column.
2. Make the verified-credit flow write a `payment_reference` row.
3. Make the inflow path idempotent on that key.
4. Prove webhook replay is idempotent.

**Acceptance criteria:** A replayed webhook produces exactly one inflow row; two different references
from the same sender for the same amount produce two rows; no code path computes a payment identity
from free-form text.
**Tests that prove it:** `capabilities.payments.dedupe-reference` from `eval/capabilities.json`; a
replay test (same payload 20 times, one row); a distinct-reference test (two rows).
**Blocks:** every "verified" claim in P1 and P2.

---

## P0-5. Secret rotation and scanner

**Why:** The 8 Oct review reported ChainRails secrets committed in task-definition JSONs. A grep of
this working tree found `chainrails` only in docs and Go source, not committed task JSON, so the
specific files are not here; rotation is still required as a precaution.

**Do:**
1. Locate the committed task-definition JSONs in their repo (not this working tree) and rotate every
   ChainRails secret they contain.
2. Move all secrets to a manager. Enforce a missing-secret fail-at-boot for webhook HMAC and Meta
   signature.
3. Turn on secret scanning in CI.

**Acceptance criteria:** No secret literal in any tracked file; the scanner fails on a planted
dummy secret; boot fails when a required webhook secret is absent.
**Tests that prove it:** a CI secret-scan job; a boot test that unsets the HMAC secret and expects a
startup failure.

---

## P0-6. WhatsApp live (start Meta verification now)

**Why:** WhatsApp is the primary channel. Meta business verification is the long pole and must start
in P0 even though the build lands later.

**Do:**
1. Start Meta business verification today.
2. Confirm the inbound signature check (`X-Hub-Signature-256`) and the bridge HMAC path with a test.
3. Confirm the platform layer accepts `whatsapp` as a first-class channel (today
   `miriam_agent/api/spectrum.py:70` lists `whatsapp` in `CHANNELS`, but it is a channel string with
   no client or webhook in Python [verified by reading that file]).

**Acceptance criteria:** An unsigned inbound is rejected; a signed inbound reaches the platform; the
WhatsApp number is attached to the bridge.
**Tests that prove it:** an inbound signature test (valid, invalid, missing); a bridge HMAC test.
**Note:** no application code changes are in scope for this task; this ticket is defined for the
build phase.

---

## P0-7. Reconciliation plus reminders for one distributor (re-cut for WhatsApp-primary)

**Why:** This is the demo and pilot loop. It is P0 in the brief and stays P0, but the channel order
flips (C1).

**Do:** Implement WF-1 (ingestion), WF-2 (reconciliation) and WF-3 (one reminder) with the network
status vocabulary and the `payment_reference` dedupe from P0-4.

**Acceptance criteria:**
- Seeded distributor view loads matched / unpaid / needs review in under 2 seconds.
- A new exact-amount test payment resolves to `matched` within 60 seconds of the webhook.
- A wrong-amount payment resolves to `needs_review / AMOUNT_MISMATCH`.
- A reminder carries the exact kobo figure; an invented figure blocks the send.
- A replayed reminder job produces one outbox row.
- 20 webhook replays change no counts.

**Tests that prove it:** the WF-1/2/3 verification cases in `MIRIAM-WORKFLOW-SPECS.md`, plus the
`capabilities.payments.dedupe-reference` case; a kobo-exact assertion test on the rendered reminder.

---

## P0 exit bar

- CI green (P0-1, P0-2, including the rewritten split-audit test).
- Capability gate merged from `eval/capability-gate` and blocking a broken trust capability (P0-3).
- Payment dedupe done: key exactly `(channel, sender_handle, amount_minor, reference)`, the table
  written by the verified-credit flow, and webhook replay proven idempotent; no "verified" claim
  elsewhere depends on a text hash (P0-4).
- No secret literals tracked; scanner live (P0-5).
- WhatsApp verification started, signatures enforced (P0-6).
- One-distributor reconciliation plus reminders demo green on WhatsApp (P0-7).
- Policy questions answered (Q1: the audit split; Q2: the table exists but is unwritten). Work still
  open: rewrite the audit test (P0-2), merge the gate (P0-3), ship the dedupe migration and wire the
  flow (P0-4).
