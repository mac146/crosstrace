"""Load one stored run and join it."""
from collectors import store
from correlator.join import correlate


def load_timeline(con, run_id, **kw):
    spans = store.load_spans(con, run_id)
    events = store.load_kernel_events(con, run_id)
    pairs = store.load_clock_sync(con, run_id)
    env = store.load_env(con, run_id)
    if not events or not pairs or env is None:
        raise ValueError(f"run {run_id} has no kernel data (was it recorded with kernel=True?)")
    return correlate(spans, events, pairs, **kw), env
