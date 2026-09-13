import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from miriam_agent.api.chat import router as chat_router
from miriam_agent.api.proactive import router as proactive_router
from miriam_agent.observability.logging import setup_logging
from miriam_agent.observability.metrics import setup_metrics
from miriam_agent.observability.tracing import setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup and shutdown."""
    # Startup
    setup_logging()
    setup_metrics()
    setup_tracing()

    print("Miriam Financial Agent API starting up...")
    print(f"Environment: {os.getenv('ENVIRONMENT', 'production')}")

    yield

    # Shutdown
    print("Miriam Financial Agent API shutting down...")


app = FastAPI(
    title="Miriam Financial Agent API",
    description=(
        "Production-grade financial agent API with memory, planning, "
        "and safe money automation"
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("ENVIRONMENT") == "development" else None,
    redoc_url="/redoc" if os.getenv("ENVIRONMENT") == "development" else None,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGINS", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add health check endpoint
@app.get("/")
async def root():
    """Liveness landing page (AtlasFlow probes this path by default)."""
    return {"service": "miriam-agent", "status": "ok", "endpoints": ["/health", "/api/v1"]}


@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "miriam-agent"}


@app.get("/health/ready")
async def ready_check():
    """Readiness probe: verifies dependencies are reachable.

    Reports per-dependency status instead of failing hard so a degraded
    path (e.g. Supermemory down) is visible but not fatal.
    """
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    checks: dict = {"status": "ready", "service": "miriam-agent"}

    # Database
    try:
        from miriam_agent.database.memory import MemoryStore

        store = MemoryStore(settings.DATABASE_URL)
        await store.initialize()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"unavailable: {e}"

    # Go backend (the money authority)
    try:
        from miriam_agent.integrations.go_client import get_go_client

        client = get_go_client()
        resp = await client._client.get("/health", timeout=3.0)
        checks["go_backend"] = (
            "ok" if resp.status_code < 500 else f"status={resp.status_code}"
        )
    except Exception as e:
        checks["go_backend"] = f"unreachable: {type(e).__name__}"

    # LLM provider
    try:
        from miriam_agent.agents.llm import get_llm_provider

        get_llm_provider()
        checks["llm"] = "ok"
    except Exception as e:
        checks["llm"] = f"unconfigured: {e}"

    return checks


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# Include routers
app.include_router(chat_router, prefix="/api/v1")
app.include_router(proactive_router, prefix="/api/v1")
