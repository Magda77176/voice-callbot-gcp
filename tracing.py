"""
OpenTelemetry Tracing — Distributed tracing for the callbot.
Each call generates a trace with spans for every step.
"""
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

resource = Resource.create({
    "service.name": "voice-callbot",
    "service.version": "2.0",
    "deployment.environment": ENVIRONMENT,
})

provider = TracerProvider(resource=resource)

if ENVIRONMENT == "production":
    try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        provider.add_span_processor(
            SimpleSpanProcessor(CloudTraceSpanExporter())
        )
    except ImportError:
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
else:
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter())
    )

trace.set_tracer_provider(provider)
tracer = trace.get_tracer("voice-callbot")


"""
Trace structure for a typical call:

call.incoming (full call duration)
  ├── call.welcome_tts
  ├── call.dtmf_collect
  ├── order.lookup
  │     └── ecommerce_api.get_order
  ├── call.status_tts
  └── call.conversation_loop
        ├── turn.1
        │     ├── sentiment.analyze
        │     ├── escalation.check
        │     ├── kb.fuzzy_match
        │     ├── gemini.generate (if no KB match)
        │     ├── cache.lookup
        │     └── tts.generate
        ├── turn.2
        │     └── ...
        └── escalation.zendesk (if triggered)
              └── zendesk_api.create_ticket
"""
