"""Prometheus metrics setup for Miriam Financial Agent."""

from prometheus_client import Counter, Histogram, Gauge, generate_latest

REQUEST_COUNT = Counter("miriam_requests_total", "Total requests", ["method", "endpoint", "status"])
REQUEST_LATENCY = Histogram("miriam_request_latency_seconds", "Request latency", ["endpoint"])
ACTIVE_CONNECTIONS = Gauge("miriam_active_connections", "Active connections")
LLM_CALLS = Counter("miriam_llm_calls_total", "LLM API calls", ["model", "status"])
LLM_LATENCY = Histogram("miriam_llm_latency_seconds", "LLM API latency", ["model"])
TOOL_EXECUTIONS = Counter("miriam_tool_executions_total", "Tool executions", ["tool", "status"])
MEMORY_OPERATIONS = Counter("miriam_memory_operations_total", "Memory operations", ["operation", "status"])


def setup_metrics() -> None:
    """Initialize metrics (no-op for now, Prometheus scrapes /metrics)."""
    pass
