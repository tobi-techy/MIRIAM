"""Prometheus metrics setup for Miriam Financial Agent."""

from prometheus_client import Counter, Gauge, Histogram

REQUEST_COUNT = Counter(
    "miriam_requests_total", "Total requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "miriam_request_latency_seconds", "Request latency", ["endpoint"]
)
ACTIVE_CONNECTIONS = Gauge("miriam_active_connections", "Active connections")
LLM_CALLS = Counter("miriam_llm_calls_total", "LLM API calls", ["model", "status"])
LLM_LATENCY = Histogram("miriam_llm_latency_seconds", "LLM API latency", ["model"])
TOOL_EXECUTIONS = Counter(
    "miriam_tool_executions_total", "Tool executions", ["tool", "status"]
)
MEMORY_OPERATIONS = Counter(
    "miriam_memory_operations_total", "Memory operations", ["operation", "status"]
)
ONBOARDING_EVENTS = Counter(
    "miriam_onboarding_events_total",
    "Conversational onboarding funnel events (interview_started, "
    "interview_finished, statement_requested, statement_provided, "
    "plan_presented, aha_generated, consent_poll, adjusting, "
    "completed_automated, completed_draft, abandoned, restarted)",
    ["user_id", "event"],
)
MIRIAM_AHA_DETECTED = Counter(
    "miriam_aha_detected_total",
    "Aha moments detected in user replies (spec v1.1 §51): the plan engine "
    "engineers the moment, this marks when it lands on the user "
    "(recognition, insight, reframe, relief)",
    ["kind", "stage"],
)


def setup_metrics() -> None:
    """Initialize metrics (no-op for now, Prometheus scrapes /metrics)."""
    pass
