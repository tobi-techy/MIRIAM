"""OpenTelemetry tracing setup for Miriam Financial Agent."""

import os


def setup_tracing() -> None:
    """Initialize OpenTelemetry tracing if configured."""
    endpoint = os.getenv("OTEL_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": "miriam-agent"})
        provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
    except ImportError:
        pass


def stamp_trace_id(trace_id: str) -> None:
    """Attribute the active span to a request trace id.

    Kept here so OpenTelemetry stays owned by this module: callers correlate
    their work without importing the OTel API themselves. A no-op when nothing
    is recording (tracing disabled, or outside a span).
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("miriam.trace_id", trace_id)
    except Exception:  # pragma: no cover - tracing must never break a request
        pass
