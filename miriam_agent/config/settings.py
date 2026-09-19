"""Configuration settings for Miriam Financial Agent."""

from functools import lru_cache

from pydantic import Field, model_validator
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
    # Optional audience/issuer pinning. Left empty by default because the Go
    # issuer does not set them; when set, decode_token enforces them instead
    # of accepting any token signed with the shared secret.
    JWT_AUDIENCE: str = Field(default="")
    JWT_ISSUER: str = Field(default="")

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
    AUTO_APPROVE_THRESHOLD: float = Field(default=100.0)
    # Server-side pending-confirmation ledger values: money actions above this
    # amount (in any of amount/amount_ngn/amount_usd) must be covered by a
    # staged confirmation record before the Go side is ever asked to move money.
    APPROVAL_REQUIRED_ABOVE: float = Field(default=2000.0)

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

    # Glider B2B API (https://docs.glider.fi/api-reference/v2-overview).
    # Direct v2 access with an x-api-key. Reads, strategy validation and draft
    # creation only -- no enrollment or withdrawal is ever agent-initiated.
    GLIDER_API_BASE_URL: str = Field(default="https://api.glider.fi/v2")
    GLIDER_API_KEY: str = Field(default="")
    GLIDER_REQUEST_TIMEOUT: float = Field(default=20.0)
    GLIDER_MAX_RETRIES: int = Field(default=2)
    # Poll cadence for async operations (Glider asks for 2-5s; a dispatched
    # operation has no SLA, so callers poll rather than assume settlement).
    GLIDER_OPERATION_POLL_SECONDS: float = Field(default=3.0)
    GLIDER_OPERATION_MAX_POLLS: int = Field(default=40)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }

    @model_validator(mode="after")
    def _guard_production_secrets(self):
        """Refuse to run in production with placeholder/weak signing secrets.

        The compose file ships ``ENVIRONMENT=production``; combined with the
        ``change-me-in-production`` defaults this silently mints JWTs any
        holder of the public repo can forge. In production, both the JWT
        signing secret and the app secret key must be strong, real values.
        Development is untouched so local runs and tests keep working.
        """
        if self.ENVIRONMENT != "production":
            return self

        weak = {"", "change-me-in-production"}
        problems: list[str] = []
        if self.JWT_SECRET in weak or len(self.JWT_SECRET) < 32:
            problems.append(
                "JWT_SECRET must be a strong, non-default value (>= 32 chars) "
                "in production"
            )
        if self.SECRET_KEY in weak or len(self.SECRET_KEY) < 32:
            problems.append(
                "SECRET_KEY must be a strong, non-default value (>= 32 chars) "
                "in production"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    return Settings()
