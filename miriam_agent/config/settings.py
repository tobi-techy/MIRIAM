"""Configuration settings for Miriam Financial Agent."""

import sys
from functools import lru_cache

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "miriam-agent"
    ENVIRONMENT: str = Field(default="development")
    DEBUG: bool = Field(default=False)
    SECRET_KEY: str = Field(default="change-me-in-production")

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://miriam:miriam_password@localhost:5432/miriam"
    )
    DATABASE_ECHO: bool = False

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # LLM
    OPENAI_API_KEY: str = Field(default="")
    OPENAI_MODEL: str = Field(default="gpt-4")
    OPENAI_TEMPERATURE: float = Field(default=0.7)
    OPENAI_MAX_TOKENS: int = Field(default=4096)
    ANTHROPIC_API_KEY: str = Field(default="")

    # Concentrate AI (primary LLM gateway). When CONCENTRATE_API_KEY is set,
    # get_llm_provider() returns the Concentrate provider and OpenAI is used
    # only as a fallback. Model routing (provider sort + model fallbacks) and
    # prompt caching are handled natively by the Concentrate Responses API.
    CONCENTRATE_API_KEY: str = Field(default="")
    CONCENTRATE_BASE_URL: str = Field(default="https://api.concentrate.ai/v1")
    CONCENTRATE_MODEL: str = Field(default="gpt-5.6-terra")
    CONCENTRATE_TEMPERATURE: float = Field(default=0.7)
    CONCENTRATE_MAX_TOKENS: int = Field(default=4096)
    CONCENTRATE_TIMEOUT: float = Field(default=60.0)
    CONCENTRATE_MAX_RETRIES: int = Field(default=3)
    # How Concentrate sorts the provider pool: "performance" (default),
    # "cost", "latency", or a live metric (e.g. "p50_latency").
    CONCENTRATE_ROUTING_SORT: str = Field(default="performance")
    # Comma-separated fallback model slugs tried after the primary model's
    # providers are exhausted (routing.model.fallbacks). "auto" is allowed.
    CONCENTRATE_FALLBACK_MODELS: str = Field(
        default="claude-sonnet-5,gemini-3.6-flash,auto"
    )
    # Prompt caching: writes an explicit cache breakpoint after the system
    # message so stable conversational prefixes are reused across turns.
    CONCENTRATE_ENABLE_CACHING: bool = Field(default=True)

    # Security
    ENCRYPTION_KEY: str = Field(default="")
    JWT_SECRET: str = Field(default="change-me-in-production")
    JWT_ALGORITHM: str = Field(default="HS256")
    JWT_EXPIRATION_MINUTES: int = Field(default=60)
    # Go access tokens (pkg/auth/jwt.go) set iss=rail_service and do not set
    # aud. Leave audience empty: a configured audience makes decode_token
    # require that claim and rejects every live Rail token. Issuer defaults
    # to the value Go writes; override it only if the issuer changes there.
    JWT_AUDIENCE: str = Field(default="")
    JWT_ISSUER: str = Field(default="rail_service")
    # Shared service credential for rail -> Python calls (e.g. the inflow
    # webhook). A user JWT must never be enough to mint ledger inflows, so
    # POST /money/inflow requires this key via the X-Rail-Service-Key header.
    # Must be set in production (guarded below); empty in development keeps
    # local runs working with the dev fallback.
    RAIL_SERVICE_KEY: str = Field(default="")
    # Demo escape hatch: let chat text like "I just got paid 100" split the
    # ledger as if the rail had reported an inflow. Off by default; the
    # production guard refuses to boot with it on. Only the rail turns text
    # into money in any real deployment.
    ALLOW_CHAT_INFLOW_SYNTH: bool = Field(default=False)

    # CORS
    ALLOWED_ORIGINS: str = Field(default="*")

    # Go Backend
    GRPC_ENDPOINT: str = Field(default="localhost:50051")
    GO_BACKEND_URL: str = Field(default="http://localhost:8080")
    # Per-request timeout for the Go money authority, and how many times a
    # *safe* call is retried (reads, and writes that carry an idempotency
    # key). A single 15s timeout with no retries turned a transient blip into
    # a hard failure the user saw as "couldn't do that".
    GO_REQUEST_TIMEOUT: float = Field(default=15.0)
    GO_MAX_RETRIES: int = Field(default=2)
    # Live Face ID confirmation cards (Go card <-> Miriam challenge join).
    # Off until the imessage e2e passes; when on, the orchestrator mints a Go
    # card best-effort after staging a challenge and falls back to the text
    # flow whenever minting fails. The flag decides routing, never authority:
    # settle always runs the existing _handle_confirm path.
    GO_CONFIRM_CARDS_ENABLED: bool = Field(default=False)
    # Comma-separated channels allowed to mint cards. iMessage owns the Face
    # ID extension; web/voice/terminal never mint.
    GO_CONFIRM_CARD_CHANNELS: str = Field(default="imessage")

    # Supermemory (long-term memory of the agent)
    # Leave empty to disable semantic memory (the agent degrades gracefully).
    SUPERMEMORY_API_KEY: str = Field(default="")
    SUPERMEMORY_BASE_URL: str = Field(default="https://api.supermemory.ai")
    SUPERMEMORY_TIMEOUT: float = Field(default=20.0)
    SUPERMEMORY_MAX_RETRIES: int = Field(default=2)

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = Field(default=60)
    RATE_LIMIT_PER_HOUR: int = Field(default=1000)

    # Safety
    MAX_DAILY_TRANSFER: float = Field(default=10000.0)
    MAX_TRANSACTION_AMOUNT: float = Field(default=5000.0)
    # The ceiling above which a money movement needs the user's confirmation.
    # Read by ``hands/limits.Policy.from_settings`` as max_auto; it is the only
    # consumer. There is no client-side or tool-side approval step.
    #
    # Zero, deliberately, and it should stay zero until the inflow webhook has
    # been right in production for a while: at zero nothing moves without a tap,
    # because every movement is above the ceiling and so becomes a challenge.
    # Raise it once Miriam's ledger has been shown to track Go's credits exactly,
    # since the ceiling is what lets her act on her own.
    APPROVAL_REQUIRED_ABOVE: float = Field(default=0.0)

    # Terminal invest testing. When true, the Spectrum endpoint accepts a
    # text-carried wallet signature on the terminal channel (ephemeral local
    # keypair, real ed25519). When false (the default, and always in any demo
    # recording) signatures arrive only through the wallet authorize flow, and
    # text-carried signatures are refused outright.
    RAIL_ALLOW_DEV_SIGN: bool = Field(default=False)

    # Whether this deployment runs exactly one process. Money turns read the
    # ledger from Redis; with more than one worker, falling back to process
    # memory during an outage would give the same user two ledgers. Leave this
    # off unless the service really is a single instance, in which case a Redis
    # outage degrades to an in-process ledger instead of refusing every turn.
    MONEY_SINGLE_PROCESS: bool = Field(default=False)

    # TypeSafe judgment layer (System One). A typed decision layer that runs
    # in front of the generator: the ingress gate classifies the turn and
    # short-circuits jailbreaks, PII pastes, and vague asks before the LLM ever
    # sees them (tool/egress gates follow). Enabled by default; the layer only
    # actually activates when TYPESAFE_API_KEY is also present, so a missing
    # key keeps the agent on the old path without any further configuration.
    TYPESAFE_ENABLED: bool = Field(default=True)
    TYPESAFE_API_KEY: str = Field(default="")
    TYPESAFE_MODEL: str = Field(default="jev-latest")
    TYPESAFE_TIMEOUT: float = Field(default=10.0)
    TYPESAFE_MAX_RETRIES: int = Field(default=3)
    # G3: the ingress `exposes_pii` question can only work if TypeSafe sees the
    # user's actual text, so the raw turn text is sent by default. Set this to
    # true to run the deterministic card/NIN/credential redactor before the
    # request (at the cost of that question's reach). The local PII
    # short-circuit still refuses obvious secrets before TypeSafe is called
    # either way, so this never disables `exposes_pii`.
    TYPESAFE_REDACT_USER_TEXT: bool = Field(default=False)

    # Proactive analyst (24/7 money watch + private outreach). The Go reacher
    # worker (RAIL_BACKEND) calls POST /api/v1/proactive/analyze to ask whether
    # there is something worth telling the user right now; Go owns quiet hours,
    # the daily cap, and iMessage delivery. These settings tune the analyst.
    PROACTIVE_ENABLED: bool = Field(default=True)
    PROACTIVE_MIN_INTERVAL_HOURS: float = Field(default=12.0)
    PROACTIVE_MAX_TOKENS: int = Field(default=700)
    PROACTIVE_TEMPERATURE: float = Field(default=0.4)

    # Miriam's three money layers (hands -> judgment -> voice), reached through
    # orchestrator.py. On means a money turn is routed to the orchestrator
    # instead of the agent loop, which is the only path that can move money.
    #
    # Turning this off does NOT restore the old writer: the money tools are not
    # in the live registry either way, so the agent loop cannot reach a rail
    # whichever way the flag is set. The flag decides routing, never authority.
    MONEY_LAYERS_ENABLED: bool = Field(default=True)

    # Conversational onboarding: the LLM-led financial interview the Python
    # brain runs before the general agent. Miriam (the LLM) carries the whole
    # conversation -- questions, statement request, plan presentation, consent --
    # while a deterministic plan builder turns her extracted answers into the
    # diagnosis, steps and standing rules. Go-rendered polls remain as optional
    # tap suggestions alongside free text. Disabling it routes every message to
    # the general agent as before.
    ONBOARDING_ENABLED: bool = Field(default=True)
    ONBOARDING_MAX_FOLLOWUPS: int = Field(default=3)
    # LLM tuning for the onboarding conductor (warm answers, not analytic).
    ONBOARDING_TEMPERATURE: float = Field(default=0.6)
    ONBOARDING_MAX_TOKENS: int = Field(default=800)
    # Hard cap on dimensions covered per interview, so the conversation always
    # reaches the plan no matter how chatty the model gets.
    ONBOARDING_MAX_QUESTIONS: int = Field(default=12)
    # How long an interview may sit idle before it resets (sliding on each
    # turn). Days.
    ONBOARDING_STATE_TTL_DAYS: int = Field(default=30)
    # When set, the plan reveal also shares a rich link to the user's plan page
    # (kind=plan) alongside the presentation. The Go executor only delivers
    # shares whose host is allowlisted via MIRIAM_SHARE_ALLOWED_HOSTS; leaving
    # this empty means no plan share is ever emitted.
    ONBOARDING_SHARE_BASE_URL: str = Field(default="")

    # Observability
    LOG_LEVEL: str = Field(default="INFO")
    OTEL_ENDPOINT: str | None = Field(default=None)
    SENTRY_DSN: str | None = Field(default=None)

    # Voice
    ELEVENLABS_API_KEY: str = Field(default="")

    # Document intelligence (Stage 3: Python processing plane).
    DOCUMENT_MAX_SIZE_MB: int = Field(default=20)
    DOCUMENT_NATIVE_PDF_ENABLED: bool = Field(default=True)
    DOCUMENT_OCR_ENABLED: bool = Field(default=True)
    DOCUMENT_OCR_URL: str = Field(default="")
    DOCUMENT_OCR_TIMEOUT_SECONDS: float = Field(default=60.0)
    DOCUMENT_LLM_EXTRACTION_ENABLED: bool = Field(default=True)
    DOCUMENT_LLM_TIMEOUT_SECONDS: float = Field(default=60.0)
    DOCUMENT_MIN_TEXT_QUALITY: int = Field(default=120)
    DOCUMENT_RECONCILIATION_TOLERANCE: str = Field(default="1.00")

    # Money pipeline (deterministic planning engines under miriam_agent/money/).
    # Thresholds live here rather than in the engines because a hardcoded limit
    # that drifts from config was a finding in the production audit; the money
    # engines read these instead. Glider is read/validate only -- the pipeline
    # never moves money, and enrollment stays user-signed and two-stage.
    MONEY_DEFAULT_COUNTRY: str = Field(default="NG")
    MONEY_DEFAULT_CURRENCY: str = Field(default="NGN")
    # The timezone whose midnight bounds the daily transfer cap. The old
    # boundary was the server's local zone, so cap resets moved with whatever
    # machine ran the process; it is now pinned to the product's home market.
    MONEY_DAY_TIMEZONE: str = Field(default="Africa/Lagos")
    # Debt triage bands (MONEY-RULES.md §3). APR >= fire is attacked; APR in the
    # judgment band is compared against the local risk-free rate from
    # money/reference.py; below that, debt is kept.
    MONEY_DEBT_FIRE_APR_PCT: float = Field(default=15.0)
    MONEY_DEBT_JUDGMENT_APR_PCT: float = Field(default=8.0)
    # How long investable money must stay untouched (R-HOUSEL-2). Money the user
    # may need inside this window is never invested.
    MONEY_INVESTABLE_HORIZON_FLOOR_MONTHS: int = Field(default=24)
    # The drawdown the book must survive without the user selling (R-HOUSEL-1).
    MONEY_DRAWDOWN_TOLERANCE_PCT: float = Field(default=40.0)

    # Glider reads go through the Go money/ledger host
    # (``integrations.go_client`` -> ``/api/v1/investments/*``), which holds
    # the only x-api-key. Python must never carry one: there is no GLIDER_API_KEY
    # here on purpose, so no code path in this repo can talk to Glider directly.

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        # Host panels often inject KEY= for every unset variable. An empty
        # string is not a valid bool or number, and it used to crash import
        # before the process could listen. Treat blank as unset so the field
        # default applies. A blank secret still fails the production guard.
        "env_ignore_empty": True,
    }

    @model_validator(mode="after")
    def _use_asyncpg_driver(self):
        """The async engine rejects a bare postgresql:// URL.

        Compose and many hosts emit the libpq form. Rewrite only that form
        so an explicit driver is left alone.
        """
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            self.DATABASE_URL = "postgresql+asyncpg://" + url[len("postgresql://") :]
        return self

    @property
    def is_production(self) -> bool:
        """True for any production spelling.

        The check used to be an exact ``== "production"``, so
        ``ENVIRONMENT=Production`` or ``prod`` silently bypassed every
        production guard below (weak secrets, wildcard CORS, default DB
        password) while operators believed the guard was on.
        """
        return self.ENVIRONMENT.strip().lower() in {"production", "prod"}

    @model_validator(mode="after")
    def _guard_production_secrets(self):
        """Refuse to run in production with placeholder/weak signing secrets.

        The compose file ships ``ENVIRONMENT=production``; combined with the
        ``change-me-in-production`` defaults this silently mints JWTs any
        holder of the public repo can forge. In production, both the JWT
        signing secret and the app secret key must be strong, real values.
        Development is untouched so local runs and tests keep working.
        """
        if not self.is_production:
            return self

        weak = {"", "change-me-in-production"}
        problems: list[str] = []

        def _is_dev_placeholder(value: str) -> bool:
            # .env.example ships dev-only secrets (e.g. dev-jwt-secret-...);
            # they are long enough to pass the length check but public, so
            # copying .env.example into production must fail loudly here.
            return value in weak or value.startswith("dev-")

        if _is_dev_placeholder(self.JWT_SECRET) or len(self.JWT_SECRET) < 32:
            problems.append(
                "JWT_SECRET must be a strong, non-default value (>= 32 chars) "
                "in production"
            )
        if _is_dev_placeholder(self.SECRET_KEY) or len(self.SECRET_KEY) < 32:
            problems.append(
                "SECRET_KEY must be a strong, non-default value (>= 32 chars) "
                "in production"
            )
        if _is_dev_placeholder(self.ENCRYPTION_KEY) or len(self.ENCRYPTION_KEY) < 32:
            problems.append(
                "ENCRYPTION_KEY must be set to a strong value (>= 32 chars) in "
                "production; deriving it from SECRET_KEY via single SHA-256 is not "
                "a KDF and must not be used in production"
            )
        if not self.JWT_ISSUER or len(self.JWT_ISSUER) < 3:
            problems.append(
                "JWT_ISSUER must be set to the issuer Go writes "
                "(rail_service) in production"
            )
        # ALLOWED_ORIGINS=* is accepted. api/main.py turns credentialed CORS
        # off in that case, and Go plus the iMessage bridge do not use CORS.
        # An explicit allowlist is still how a browser app gets credentials.
        if ":miriam_password@" in self.DATABASE_URL:
            problems.append(
                "DATABASE_URL must not contain the default password 'miriam_password' "
                "in production; inject via secrets"
            )
        if _is_dev_placeholder(self.RAIL_SERVICE_KEY) or (
            len(self.RAIL_SERVICE_KEY) < 32
        ):
            problems.append(
                "RAIL_SERVICE_KEY must be a strong value (>= 32 chars) in "
                "production; it is the only credential allowed to mint ledger "
                "inflows"
            )
        if self.ALLOW_CHAT_INFLOW_SYNTH:
            problems.append(
                "ALLOW_CHAT_INFLOW_SYNTH must be false in production: chat text "
                "must never mint ledger inflows"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    try:
        return Settings()
    except ValidationError as exc:
        # The traceback's last frame is validate_python, and the reason is a
        # later log line that deploy viewers drop. Print the field messages
        # only: error input can contain the secrets that failed the check.
        reasons: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ())) or "settings"
            reasons.append(f"{loc}: {err.get('msg', 'invalid')}")
        print(
            "miriam refused to start: " + "; ".join(reasons),
            file=sys.stderr,
            flush=True,
        )
        raise
