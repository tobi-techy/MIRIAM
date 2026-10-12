# Production readiness

What must be true before MIRIAM serves real money, and how each belt is wired.

## Deploy gate

MIRIAM deploys only through **AtlasFlow** (`README.md`; `docker-compose.yml` is
the source of truth for the container shape). There is no deploy workflow and no
`fly.toml`/`k8s`/`helm`/`terraform` manifest in this repo, so the gate is not a
deploy step here — it is a required status check on `main` that AtlasFlow (or a
human merging) cannot skip.

`capability-gate` runs `python -m eval.capability_catalog`: it executes the
evidence behind every capability `eval/capabilities.json` claims as proven and
holds when any area falls below its bar in `eval/capability_policy.py` (trust
areas: 100%). It must be green on the exact revision being deployed.

### Required status checks

Make these checks required on `main` (names are the GitHub check-run names from
`.github/workflows/ci.yml`):

| Check | Covers |
|---|---|
| `test (3.11)` | full suite on Python 3.11 |
| `test (3.12)` | full suite on Python 3.12 |
| `trust-gates (3.12)` | grounding, limits, exactly-once, shadow parity, hallucination eval |
| `capability-gate` | the capability catalog above |
| `security-scan` | `pip-audit` (project deps) and `semgrep` |

Setup, either in **Settings → Branches → Branch protection rules → `main`**
(enable "Require status checks to pass before merging" and add the five above,
with "Require branches to be up to date before merging"), or with the helper:

```console
scripts/require_branch_protection.sh            # defaults to tobi-techy/MIRIAM main
```

These five checks were applied to `main` with that helper (strict mode, no
required reviews). Re-run it after renaming a job or adding a new gate.

The helper uses `PATCH .../branches/main/protection/required_status_checks`, so
it only touches the required-checks list and leaves other protection settings
(reviews, enforce-admins, ...) alone. It needs a token with `administration:write`.

## Migrations and readiness

- `entrypoint.sh` does **not** migrate; the deploy job runs `alembic upgrade head`
  before containers start. A failed migration must stop the deploy.
- Readiness/liveness probe is `GET /health/ready`; `docker-compose.yml` gates the
  app on Postgres and Redis health before starting it.

## Secrets and configuration

Two independent layers already fail fast, and one gap remains.

### The app refuses to boot in production without these

`miriam_agent/config/settings.py::_guard_production_secrets` raises on import
when `ENVIRONMENT` is `production`/`prod` (any casing) and:

| Variable | Rule |
|---|---|
| `JWT_SECRET` | strong, not the `change-me-in-production` default or a `dev-` value, `>= 32` chars |
| `SECRET_KEY` | same |
| `ENCRYPTION_KEY` | strong, `>= 32` chars (no deriving it from `SECRET_KEY`) |
| `JWT_ISSUER` | set, `>= 3` chars (Go writes `rail_service`) |
| `RAIL_SERVICE_KEY` | strong, `>= 32` chars — the only credential that may mint ledger inflows |
| `DATABASE_URL` | must not contain the default `miriam_password` |
| `ALLOW_CHAT_INFLOW_SYNTH` | must be `false` (chat text must never mint inflows) |

### Compose requires these to be present at all

`docker-compose.yml` uses `${VAR:?message}` so a missing value aborts
`docker compose up` with a named error: `POSTGRES_PASSWORD`, `SECRET_KEY`,
`JWT_SECRET`, `ENCRYPTION_KEY`, `RAIL_SERVICE_KEY`.

### Not guarded — confirm by hand before go-live

| Variable | Why it matters |
|---|---|
| `OPENAI_API_KEY` **or** `CONCENTRATE_API_KEY` | Without one, building the agent raises `ConfigurationError` and every answerable chat turn returns 500. Not covered by the production guard. |
| `GO_BACKEND_URL` | Defaults to `http://localhost:8080`; must point at the Go service or no money moves. |
| `RAIL_ALLOW_DEV_SIGN` | Defaults `false`. Keep it false in production: `true` lets a base64 blob in a `terminal` message settle a wallet signature. |
| `MONEY_SINGLE_PROCESS` | Defaults `false`. Must match reality: `true` only for a genuinely single-replica deploy, otherwise a Redis outage gives one user two ledgers. AtlasFlow runs `replicas: 1`. |
| `ALLOWED_ORIGINS` | Defaults to `*`, which turns credentialed CORS off (`api/main.py`). Set the real origin list only if a browser app needs cookies. |
| `SUPERMEMORY_API_KEY` | Memory graph; without it the person-memory features degrade. |
| `TYPESAFE_API_KEY` | Optional; without it the judgment layer runs the pre-TypeSafe path. `TYPESAFE_ENABLED` defaults `true`. |
| `ELEVENLABS_API_KEY` | Voice. |
| `OTEL_ENDPOINT`, `SENTRY_DSN` | Observability; both default unset. |
| `SPECTRUM_*`, `GO_CONFIRM_CARDS_ENABLED`, `PROACTIVE_ENABLED` | Feature flags; confirm the intended values per environment. |

`.env.example` lists the full surface for local development; never copy its
`dev-*` values into production (the guard rejects them).

## Follow-ups worth opening

- Add `OPENAI_API_KEY`/`CONCENTRATE_API_KEY` and `RAIL_ALLOW_DEV_SIGN` to the
  production guard so a misconfigured host fails at boot instead of at the first
  money turn.
- `tests/test_spec_examples.py` skips 13 tests because
  `docs/miriam_spec/miriam_spec_v1.2.md` is missing and a bare
  `except Exception: pytest.skip` hides it; the spec-gate CI step passes
  vacuously as a result.
- Dead deploy-target references still appear in docs (AWS ECS, Fly, k8s,
  Terraform) and conflict with the AtlasFlow-only rule; archive them.
