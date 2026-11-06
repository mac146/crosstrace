"""OpenTelemetry tracing, hand-instrumented (OTel GenAI + HTTP + DB semantic conventions).

Every span carries process.pid and thread.id, which is what the Stage 3 join uses. Timestamps are OTel's wall
clock (time.time_ns()). Spans are kept in memory and written to the SQLite store by the run harness; there is no
separate OTel collector process (see docs/decisions.md D8).

Two modes:
  Tracing()              (default) ONE process-wide TracerProvider, created on first use, like a real
                         application. Each Tracing object is one run: its spans are tagged with a run number and
                         collected into a per-run bucket that spans() empties.
  Tracing(shared=False)  the original design: a new TracerProvider per run. Kept only so the overhead benchmark
                         can show what that cost (see docs/decisions.md D22).
"""
import contextlib
import itertools
import os
import threading

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

RUN_ATTR = "crosstrace.run"


def span_to_dict(s):
    parent = s.parent.span_id if s.parent else None
    return {"span_id": format(s.context.span_id, "016x"),
            "parent_id": format(parent, "016x") if parent else None,
            "name": s.name, "start_ns": s.start_time, "end_ns": s.end_time,
            "attrs": {k: v for k, v in (s.attributes or {}).items() if k != RUN_ATTR}}


class _RunBuckets(SpanExporter):
    """Collects finished spans per run number. Spans of a run are released by pop()."""

    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()

    def export(self, spans):
        with self._lock:
            for s in spans:
                self._buckets.setdefault((s.attributes or {}).get(RUN_ATTR), []).append(s)
        return SpanExportResult.SUCCESS

    def pop(self, run):
        with self._lock:
            return self._buckets.pop(run, [])

    def shutdown(self):
        pass


_shared = None
_shared_lock = threading.Lock()
_run_numbers = itertools.count(1)


def _shared_state():
    global _shared
    with _shared_lock:
        if _shared is None:
            buckets = _RunBuckets()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(buckets))
            _shared = (provider.get_tracer("crosstrace.agent"), buckets)
        return _shared


class Tracing:
    def __init__(self, shared=True):
        self._shared = shared
        if shared:
            self._tracer, self._buckets = _shared_state()
            self._run = next(_run_numbers)
            self._released = []
        else:
            self._memory = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(self._memory))
            self._tracer = provider.get_tracer("crosstrace.agent")

    @contextlib.contextmanager
    def span(self, name, attrs=None, client=False):
        attributes = {**(attrs or {}), "process.pid": os.getpid(), "thread.id": threading.get_native_id()}
        if self._shared:
            attributes[RUN_ATTR] = self._run
        with self._tracer.start_as_current_span(
                name, kind=SpanKind.CLIENT if client else SpanKind.INTERNAL,
                attributes=attributes) as s:
            yield s

    def spans(self):
        """All finished spans of this run, repeatable. In shared mode the global bucket is drained into
        this object on each call, so the process-wide exporter does not grow without bound."""
        if self._shared:
            self._released.extend(self._buckets.pop(self._run))
            raw = self._released
        else:
            raw = self._memory.get_finished_spans()
        return sorted((span_to_dict(s) for s in raw), key=lambda s: (s["start_ns"], -s["end_ns"]))


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
