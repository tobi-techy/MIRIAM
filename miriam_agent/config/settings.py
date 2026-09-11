"""Configuration settings for Miriam Financial Agent."""

from functools import lru_cache

from pydantic import Field
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

    # CORS
    ALLOWED_ORIGINS: str = Field(default="*")

    # Go Backend
    GRPC_ENDPOINT: str = Field(default="localhost:50051")
    GO_BACKEND_URL: str = Field(default="http://localhost:8080")

    # Supermemory (long-term memory of the agent)
    # Leave empty to disable semantic memory (the agent degrades gracefully).
    SUPERMEMORY_API_KEY: str = Field(default="")
    SUPERMEMORY_BASE_URL: str = Field(default="https://api.supermemory.ai")
    SUPERMEMORY_TIMEOUT: float = Field(default=20.0)
    SUPERMEMORY_MAX_RETRIES: int = Field(default=2)

    # Vector Search
    EMBEDDING_MODEL: str = Field(default="text-embedding-3-small")
    EMBEDDING_DIMENSIONS: int = Field(default=1536)

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = Field(default=60)
    RATE_LIMIT_PER_HOUR: int = Field(default=1000)

    # Safety
    MAX_DAILY_TRANSFER: float = Field(default=10000.0)
    MAX_TRANSACTION_AMOUNT: float = Field(default=5000.0)
    AUTO_APPROVE_THRESHOLD: float = Field(default=100.0)

    # Observability
    LOG_LEVEL: str = Field(default="INFO")
    OTEL_ENDPOINT: str | None = Field(default=None)
    SENTRY_DSN: str | None = Field(default=None)

    # Voice
    ELEVENLABS_API_KEY: str = Field(default="")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    return Settings()
