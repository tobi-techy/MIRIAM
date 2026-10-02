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
    ["event"],
)
AGENT_BUDGET_EXCEEDED = Counter(
    "miriam_agent_budget_exceeded_total",
    "Agent per-task budget aborts (steps/tokens/cost/wall_clock)",
    ["budget"],
)
AGENT_COST_USD = Counter(
    "miriam_agent_cost_usd_total",
    "Estimated LLM cost in USD",
    ["model"],
)
MIRIAM_AHA_DETECTED = Counter(
    "miriam_aha_detected_total",
    "Aha moments detected in user replies (spec v1.2 §51): the plan engine "
    "engineers the moment, this marks when it lands on the user "
    "(recognition, insight, reframe, relief)",
    ["kind", "stage"],
)


def setup_metrics() -> None:
    """Initialize metrics (no-op for now, Prometheus scrapes /metrics)."""
    pass


def record_llm_call(
    model: str, status: str, latency_seconds: float | None = None
) -> None:
    """Increment the LLM call counters. Never raises: observability must not
    break the request path it is observing."""
    try:
        LLM_CALLS.labels(model or "", status).inc()
        if latency_seconds is not None:
            LLM_LATENCY.labels(model or "").observe(latency_seconds)
    except Exception:
        pass


def record_tool_execution(tool: str, status: str) -> None:
    """Increment the tool-execution counters. Never raises."""
    try:
        TOOL_EXECUTIONS.labels(tool or "", status).inc()
    except Exception:
        pass


REPLY_GUARD = Counter(
    "miriam_reply_guard_total",
    "Reply-guard decisions before a reply is sent",
    ["rule", "outcome"],
)


def record_reply_guard(rule: str, outcome: str) -> None:
    """Count one reply-guard decision. Never raises.

    ``rule`` is the guard rule that fired (or "none" for an accepted reply) and
    ``outcome`` is ``allowed``, ``blocked``, ``retried`` or ``fell_back``. This
    is the runtime half of the hallucination measurement: the eval set says what
    the guard catches in CI, this says how often it fires in production.
    """
    try:
        REPLY_GUARD.labels(rule or "none", outcome).inc()
    except Exception:
        pass
