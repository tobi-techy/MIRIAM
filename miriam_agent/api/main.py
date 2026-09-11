import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from miriam_agent.api.chat import router as chat_router
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
    description="Production-grade financial agent API with memory, planning, and safe money automation",
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
@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring."""
    return {"status": "healthy", "service": "miriam-agent"}

# Include routers
app.include_router(chat_router, prefix="/api/v1")
