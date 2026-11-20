from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from fastapi import FastAPI


def configure_telemetry(app: FastAPI, service_name: str) -> bool:
    """Enable OTLP tracing only when an endpoint is explicitly configured."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "semora",
                "deployment.environment.name": os.getenv("SEMORA_ENVIRONMENT", "development"),
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"),
        )
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")

    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
    except ImportError:
        pass
    # Database auto-instrumentation is deliberately not enabled. SQL and bound
    # parameters belong in Semora's access-controlled audit record, not in a
    # general-purpose observability backend.
    return True


@contextmanager
def traced_operation(name: str, attributes: dict[str, str] | None = None) -> Iterator[object | None]:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        yield None
        return
    from opentelemetry import trace

    with trace.get_tracer("semora.workflow").start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        yield span
