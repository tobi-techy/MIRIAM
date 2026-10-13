# Miriam Financial Agent

A production-grade financial agent built in Python that:
- Understands financial data and provides insights
- Builds plans and strategies
- Automates money safely with user approval
- Maintains good memory of conversations and user history
- Is conversational and proactive
- Targets 99%+ reliability for production use

## Architecture Overview

This is a bounded modular monolith designed for production safety and scalability:

```
flowchart TD
    A[User Chat] --> B[Conversation Layer]
    B --> C[Agent Orchestrator]
    C --> D[Memory Service]
    C --> E[Reasoning Engine]
    E --> F[Safety & Policy Layer]
    F --> G[Financial Intelligence]
    G --> H[Integration Gateway]
    H --> I[Go Payment Service gRPC]
    H --> J[Plaid/ACH]
    H --> K[Crypto Wallets]
    F --> L[Audit & Compliance]
    D --> M[PostgreSQL + Vector Store]
    G --> N[Analytics Engine]
```

## Key Features

- **Safety First**: All money actions require explicit user approval
- **Immutable Audit Trail**: Financial actions are recorded and cannot be altered
- **Fail Closed**: System stops rather than guessing when components are degraded
- **Idempotency**: Every external action is safe to retry
- **Proactive**: Scheduled check-ins, anomaly alerts, plan reminders

## Deployment Status

Currently in Phase 1: Production Foundations
- [ ] Python service scaffolding
- [ ] Authentication and authorization
- [ ] Observability (logging, metrics, tracing)
- [ ] Database setup
- [ ] Vector memory layer
- [ ] Safety boundaries

## Deployment Targets

MIRIAM Python deploys only via **AtlasFlow**; `docker-compose.yml` is the shape
of the container AtlasFlow runs. There is no `fly.toml`/`k8s`/`helm`/`terraform`
manifest in this repo and none should be added — AtlasFlow is the only deploy
target.

There is **no separate AtlasFlow project for this Python agent**. AtlasFlow runs
the Go service (`RAIL-BACKEND-SERVICE`, the authority for money, records and
consent); it reaches this agent over HTTP when `PYTHON_AGENT_ENABLED=true` and
`PYTHON_AGENT_URL` points at it, with a `JWT_SECRET` aligned to Go's. The agent
is deployed as its own container and called by Go — it does not own money or
consent, and it is not a second deploy target.

### Deploy contract (what the environment must provide)

AtlasFlow injects environment variables per environment; their names must match
`^[A-Z_][A-Z0-9_]*$`. There is no launchfile — the `Dockerfile` is the whole
build definition. The deploy must provide, by name:

| Variable | Why |
|---|---|
| `PORT` | The port uvicorn listens on. **Set `PORT=8000`**: AtlasFlow probes `http://<vm>:<PORT>/` and defaults `PORT` to `3000`, so an unset `PORT` leaves the app on 8000 while the platform probes 3000 and the deploy never goes healthy. |
| `ENVIRONMENT=production` | Turns on the production guards (weak-secret, `GO_BACKEND_URL`, model-key, chat-inflow checks). |
| `DATABASE_URL` | Postgres URL. The database must have the **`vector` extension available**: use a `pgvector`-capable Postgres (the `pgvector/pgvector:pg15` image, or a managed Postgres with pgvector enabled). `postgres:15` alone cannot run migration 001. |
| `REDIS_URL` | Ledger, rate-limit and onboarding state. |
| `JWT_SECRET`, `SECRET_KEY`, `ENCRYPTION_KEY` | Strong, ≥32 chars, not the `dev-*` values. `JWT_SECRET` must equal Go's. |
| `RAIL_SERVICE_KEY` | The only credential allowed to mint ledger inflows. Must equal Go's. |
| `GO_BACKEND_URL` | The Go money authority. Must not be the `localhost:8080` default; the container refuses to boot with it. |
| `OPENAI_API_KEY` **or** `CONCENTRATE_API_KEY` | The model key. The container refuses to boot without one; without it every answerable turn 500s. |

Health: AtlasFlow probes `http://<vm>:<PORT>/` (`GET /` → 200). Readiness is
`GET /health/ready`, which reports `database`, `go_backend`, `llm` and `config`.
Migrations run as a pre-start step (`alembic upgrade head`), not from
`entrypoint.sh`; the image ships `alembic/`, `alembic.ini` and `scripts/` so the
migration can run from inside the container.
