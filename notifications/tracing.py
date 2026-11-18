"""Traces OpenTelemetry, envoyées à Jaeger.

Ce fichier est identique dans les six services. Les traces ne sont activées que si
OTEL_EXPORTER_OTLP_ENDPOINT est définie (par exemple http://jaeger:4318). Elles partent
en arrière-plan, par lots : si Jaeger est injoignable, elles sont perdues, mais le
service continue de répondre normalement.
"""
import logging
import os

log = logging.getLogger("tracing")

# Requêtes trop fréquentes et sans intérêt pour suivre un parcours : sondes et métriques.
EXCLUDED_URLS = "/live,/health,/healthz,/metrics,/static"


def setup_tracing(app, service_name: str, engine=None) -> bool:
    """Trace les requêtes reçues (FastAPI), les appels sortants (httpx) et, si un moteur
    est fourni, les requêtes SQL (SQLAlchemy). Renvoie True si les traces sont actives."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import Decision, ParentBased, Sampler, SamplingResult
        from opentelemetry.trace import SpanKind
    except ImportError as exc:
        log.warning("Traces désactivées, bibliothèque OpenTelemetry absente : %s", exc)
        return False

    class RequestsOnly(Sampler):
        """Ne commence une trace que pour une requête reçue. Les tâches de fond (comme la
        boîte d'envoi d'orders, toutes les 3 secondes) ne créent donc pas de traces isolées
        qui chasseraient les vraies de la mémoire de Jaeger."""

        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                          links=None, trace_state=None):
            decision = Decision.RECORD_AND_SAMPLE if kind == SpanKind.SERVER else Decision.DROP
            return SamplingResult(decision, attributes)

        def get_description(self):
            return "RequestsOnly"

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})
    # Une étape issue d'une requête suit la décision de sa requête ; une étape sans requête
    # d'origine est écartée.
    provider = TracerProvider(resource=resource, sampler=ParentBased(root=RequestsOnly()))
    # L'adresse vient de OTEL_EXPORTER_OTLP_ENDPOINT, complétée de /v1/traces.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # Sans les étapes internes « send » et « receive » d'ASGI, qui n'apprennent rien.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=EXCLUDED_URLS,
                                       exclude_spans=["receive", "send"])
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    log.info("Traces actives, envoyées à %s", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    return True
