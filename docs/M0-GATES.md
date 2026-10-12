# M0 Gates — Stop the Bleeding (wk 1–2)

How to verify the 13 M0 fixes block a merge.

## Quick run

```bash
# Python (MIRIAM)
uv sync --extra dev
uv run pytest tests/test_classifier_golden.py tests/test_synth_gate.py -v
uv run ruff check . && uv run ruff format --check .
uv run alembic upgrade head --sql | head -n 20  # no DB required
uv run alembic check  # requires DB; in CI it has one

# Go (RAIL_BACKEND, in /tmp/rail-m0 or main checkout)
make lint
make test
```

## Gate matrix

| Gate | Command | What it proves |
|---|---|---|
| Classifier golden | `pytest tests/test_classifier_golden.py` | Pidgin/typo/Hausa/photo-caption routing; low-confidence → ask (G21) |
| Synth flag | `pytest tests/test_synth_gate.py` + `ENVIRONMENT=production ALLOW_CHAT_INFLOW_SYNTH=true` boot failure | Chat text cannot mint ledger money outside dev (G23) |
| PaymentReference | `alembic upgrade --sql` shows `uq_payment_ref_dedupe` index | Inflow dedupes on structured columns not raw text hash (G23) |
| Audit durability | orchestrator unit test: sink that always raises → handle raises `LedgerUnavailable` (503) not 500 | Settled money without a durable row is impossible; caller retries (G25) |
| Single-uvicorn | `grep -c 'uvicorn' entrypoint.sh` == 1, `exec uvicorn` with `--timeout-graceful-shutdown` | No divergent in-process ledgers during Redis outage (G22) |
| Conflict retry | `POST /api/v1/chat` concurrent confirms: second returns receipt not 500 | Lost-update retries ×3 before 409 (G22) |
| Alembic source-of-truth | `alembic check` green | `create_all` vs `init-db.sql` reconciled; new DDL goes via migration (G27) |
| Compose readiness | `docker compose config` shows `condition: service_healthy` and healthcheck `/health/ready` | Deploys gate on DB/Go/LLM readiness, not liveness (G32) |
| Go: pending commit | `go test -run TestCommitPending -race` | `CommitPendingTransaction` uses conditional `UPDATE … WHERE status='pending'` (G5) |
| Go: deterministic keys | grep no `UnixNano` in `service.go` + `go test -run TestIdempotency -race` | Retries reuse the same key (G6) |
| Go: velocity | `go test -run TestVelocity` with `Africa/Lagos` bucket | Per-currency limits, Lagos-day bucket, trip metrics (G7) |
| Go: outbox | `go test ./internal/workers/ledger_outbox_publisher` + DLQ table | Events not lost to logs; DLQ + alerts (G4) |
| Go: internal throttle | `grep TimeoutMiddleware.*internal` | `/internal` has timeout + documented retry contract (G16) |

## Manual checks (no automation yet)

- `InternalRequestSignature` secret set in prod (RAIL side, `INTERNAL_REQUEST_SIGNING_SECRET` ≥32 chars).
- WhatsApp creds: `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` present; Meta webhook registered.
- `MONEY_SINGLE_PROCESS` only true on single-replica deploys (AtlasFlow `replicas: 1`).
