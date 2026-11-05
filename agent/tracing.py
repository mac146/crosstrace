"""Minimal span recorder for Stage 0.

Stage 1 replaces this with OpenTelemetry GenAI spans. The fields (name, kind, start/end,
pid, tid, attrs) are chosen to map directly onto OTel so the swap is mechanical.
Timestamps are time.time_ns() (wall clock); clock alignment with kernel events is a Stage 3 topic.
"""
import contextlib
import os
import threading
import time


class Tracer:
    def __init__(self):
        self.spans = []
        self._stack = []

    @contextlib.contextmanager
    def span(self, name, kind, **attrs):
        s = {"name": name, "kind": kind, "start_ns": time.time_ns(), "end_ns": None,
             "pid": os.getpid(), "tid": threading.get_native_id(),
             "parent": self._stack[-1]["name"] if self._stack else None,
             "depth": len(self._stack), "attrs": dict(attrs)}
        self.spans.append(s)
        self._stack.append(s)
        try:
            yield s
        finally:
            s["end_ns"] = time.time_ns()
            self._stack.pop()

    def render(self):
        t0 = min(s["start_ns"] for s in self.spans)
        lines = []
        for s in self.spans:
            dur = (s["end_ns"] - s["start_ns"]) / 1e6
            off = (s["start_ns"] - t0) / 1e6
            lines.append(f"{'  ' * s['depth']}{s['kind']:<13} {s['name']:<22} "
                         f"+{off:8.1f}ms  {dur:8.1f}ms  pid={s['pid']} tid={s['tid']}")
        return "\n".join(lines)
