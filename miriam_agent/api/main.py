import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from miriam_agent.api.chat import router as chat_router
from miriam_agent.api.proactive import router as proactive_router
from miriam_agent.api.spectrum import router as spectrum_router
from miriam_agent.observability.correlation import (
    TRACE_HEADER,
    bind_trace_id,
    current_trace_id,
    new_trace_id,
    normalize_trace_id,
)
from miriam_agent.observability.logging import setup_logging
from miriam_agent.observability.metrics import (
    REQUEST_COUNT,
    REQUEST_LATENCY,
    setup_metrics,
)
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

def _cors_origins() -> list[str]:
    """Parse ``ALLOWED_ORIGINS`` into a list of origins.

    The raw value was used verbatim as a single origin, so a comma-separated
    allowlist became one nonsense origin that no browser would ever match.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "*") or "*"
    return [origin.strip() for origin in raw.split(",") if origin.strip()] or ["*"]


_cors_origins_list = _cors_origins()
_cors_wildcard = "*" in _cors_origins_list

# CORS middleware. A wildcard origin combined with `allow_credentials=True` is
# rejected by browsers and, where it is honoured, lets any site make
# credentialed calls. Credentials are therefore only enabled for an explicit
# allowlist; with `*` the API stays readable cross-origin without carrying the
# caller's session.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_list,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

if _cors_wildcard:
    logging.getLogger(__name__).warning(
        "ALLOWED_ORIGINS is '*' -- CORS credentials are disabled and any "
        "origin may read responses. Set an explicit allowlist in production."
    )


@app.middleware("http")
async def correlate_requests(request: Request, call_next):
    """Adopt the channel's trace id, or mint one, and echo it back.

    This is the top of the request: the channel adapter (the Go bridge relaying
    iMessage/WhatsApp, or a web client) may mint ``X-Miriam-Trace-Id`` and the
    id is adopted rather than replaced, so a message can be followed across
    systems. Everything below -- orchestrator, tool registry, safety audit,
    onboarding trace, OTel spans -- records the same value, and it comes back on
    the response so a caller can correlate replies with its own records.
    """
    trace_id = normalize_trace_id(request.headers.get(TRACE_HEADER)) or new_trace_id()
    endpoint = request.url.path
    start = time.perf_counter()
    with bind_trace_id(trace_id) as bound:
        response = await call_next(request)
    response.headers[TRACE_HEADER] = bound
    try:
        REQUEST_COUNT.labels(
            request.method, endpoint, str(response.status_code)
        ).inc()
        REQUEST_LATENCY.labels(endpoint).observe(time.perf_counter() - start)
    except Exception:
        pass
    return response


# Add health check endpoint
@app.exception_handler(StarletteHTTPException)
async def http_exception_with_trace(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Return the standard error body with the request's trace id attached.

    The middleware already echoes the id as a header, but a failed call (401,
    404, 429) came back with no id in the body, so a client that only logs the
    body could not join the failure to the server-side records.
    """
    trace_id = current_trace_id()
    headers = {TRACE_HEADER: trace_id} if trace_id else None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "trace_id": trace_id},
        headers=headers,
    )


@app.get("/")
async def root():
    """Liveness landing page (AtlasFlow probes this path by default)."""
    return {
        "service": "miriam-agent",
        "status": "ok",
        "endpoints": ["/health", "/api/v1"],
    }


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
app.include_router(spectrum_router, prefix="/api/v1")
