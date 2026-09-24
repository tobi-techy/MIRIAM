import logging
import os
import secrets
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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup and shutdown."""
    # Startup
    setup_logging()
    setup_metrics()
    setup_tracing()

    logger.info("Miriam Financial Agent API starting up")
    logger.info("Environment: %s", os.getenv("ENVIRONMENT", "production"))

    yield

    # Shutdown
    logger.info("Miriam Financial Agent API shutting down")


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

    Read from Settings (the single config source) rather than a raw
    ``os.getenv`` that could disagree with what the production guard checks.
    The raw value used to be used verbatim as a single origin, so a
    comma-separated allowlist became one nonsense origin no browser would
    ever match.
    """
    from miriam_agent.config.settings import get_settings

    raw = get_settings().ALLOWED_ORIGINS or "*"
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
async def add_security_headers(request: Request, call_next):
    """Add baseline security headers to every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
    )
    # HSTS only makes sense behind TLS; harmless to send always
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


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
        REQUEST_COUNT.labels(request.method, endpoint, str(response.status_code)).inc()
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
    path (e.g. Supermemory down) is visible but not fatal. Probes reuse the
    shared singletons rather than constructing fresh clients per scrape.
    """
    checks: dict = {"status": "ready", "service": "miriam-agent"}

    # Database — the shared store, initialized once and reused by probes.
    try:
        from miriam_agent.api.dependencies import get_or_init_memory_store

        await get_or_init_memory_store()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"unavailable: {type(e).__name__}"

    # Go backend (the money authority)
    try:
        from miriam_agent.integrations.go_client import get_go_client

        status_code = await get_go_client().health()
        checks["go_backend"] = "ok" if status_code < 500 else f"status={status_code}"
    except Exception as e:
        checks["go_backend"] = f"unreachable: {type(e).__name__}"

    # LLM provider
    try:
        from miriam_agent.agents.llm import get_llm_provider

        get_llm_provider()
        checks["llm"] = "ok"
    except Exception as e:
        checks["llm"] = f"unconfigured: {type(e).__name__}"

    return checks


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus metrics endpoint — restricted in production.

    Exposed without auth in development for scraping; in production a caller
    must present the rail service key (``X-Rail-Service-Key``) or a JWT
    carrying an admin/metrics role. A plain user token is refused: request
    counts, latencies and dependency state are infrastructure facts, not
    user-readable data. Unauthenticated external access returns 404 to avoid
    leaking the endpoint's existence via enumeration.
    """
    from miriam_agent.auth.jwt import decode_token, has_role
    from miriam_agent.config.settings import get_settings

    settings = get_settings()
    if settings.is_production:
        valid = False
        key = request.headers.get("X-Rail-Service-Key", "")
        if (
            key
            and settings.RAIL_SERVICE_KEY
            and secrets.compare_digest(key, settings.RAIL_SERVICE_KEY)
        ):
            valid = True
        if not valid:
            auth = request.headers.get("Authorization", "")
            scheme, _, token = auth.partition(" ")
            if scheme == "Bearer" and token.strip():
                try:
                    payload = decode_token(token.strip())
                    valid = has_role(payload, "admin") or has_role(payload, "metrics")
                except Exception:
                    valid = False
        if not valid:
            # Return 404 (not 401) to avoid confirming the endpoint exists to scanners.
            return JSONResponse(status_code=404, content={"detail": "Not found"})
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# Include routers
app.include_router(chat_router, prefix="/api/v1")
app.include_router(proactive_router, prefix="/api/v1")
app.include_router(spectrum_router, prefix="/api/v1")
