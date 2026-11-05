"""OpenTelemetry tracing, hand-instrumented (OTel GenAI + HTTP + DB semantic conventions).

One TracerProvider per run (no global state). Every span carries process.pid and thread.id,
which is what the Stage 3 join will use. Timestamps are OTel's wall clock (time.time_ns()).

Spans are kept in memory and written to the SQLite store by the run harness; there is no
separate OTel collector process in Stage 1 (see docs/decisions.md D8).
"""
import contextlib
import os
import threading

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind


def span_to_dict(s):
    parent = s.parent.span_id if s.parent else None
    return {"span_id": format(s.context.span_id, "016x"),
            "parent_id": format(parent, "016x") if parent else None,
            "name": s.name, "start_ns": s.start_time, "end_ns": s.end_time,
            "attrs": dict(s.attributes or {})}


class Tracing:
    def __init__(self):
        self._memory = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self._memory))
        self._tracer = provider.get_tracer("crosstrace.agent")

    @contextlib.contextmanager
    def span(self, name, attrs=None, client=False):
        attributes = {**(attrs or {}), "process.pid": os.getpid(), "thread.id": threading.get_native_id()}
        with self._tracer.start_as_current_span(
                name, kind=SpanKind.CLIENT if client else SpanKind.INTERNAL,
                attributes=attributes) as s:
            yield s

    def spans(self):
        out = [span_to_dict(s) for s in self._memory.get_finished_spans()]
        return sorted(out, key=lambda s: (s["start_ns"], -s["end_ns"]))


def render(spans):
    """Indented text view of a span list (list of dicts)."""
    depth = {}
    by_id = {s["span_id"]: s for s in spans}
    t0 = min(s["start_ns"] for s in spans)
    lines = []
    for s in spans:
        d = depth[s["span_id"]] = (depth.get(s["parent_id"], -1) + 1) if s["parent_id"] in by_id else 0
        lines.append(f"{'  ' * d}{s['name']:<26} +{(s['start_ns'] - t0) / 1e6:9.1f}ms "
                     f"{(s['end_ns'] - s['start_ns']) / 1e6:9.1f}ms "
                     f"pid={s['attrs'].get('process.pid')} tid={s['attrs'].get('thread.id')}")
    return "\n".join(lines)


class _NoopSpan:
    def set_attribute(self, *a, **k):
        pass


class NoopTracing:
    """Same interface as Tracing, records nothing. The 'untraced' arm of the overhead measurement."""

    @contextlib.contextmanager
    def span(self, name, attrs=None, client=False):
        yield _NoopSpan()

    def spans(self):
        return []
