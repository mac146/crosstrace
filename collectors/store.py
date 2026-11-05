"""SQLite store for runs, spans and ground truth.

The database lives on the WSL-native filesystem by default (see docs/decisions.md D2/D7),
override with the CROSSTRACE_DATA environment variable.
"""
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, fault_type TEXT NOT NULL, seed INTEGER NOT NULL,
    split TEXT NOT NULL, started_ns INTEGER, ended_ns INTEGER, status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS spans (
    run_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
    start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, pid INTEGER, tid INTEGER,
    attrs TEXT NOT NULL, PRIMARY KEY (run_id, span_id));
CREATE TABLE IF NOT EXISTS ground_truth (
    run_id TEXT PRIMARY KEY, fault_type TEXT NOT NULL, target TEXT NOT NULL,
    params TEXT NOT NULL, start_ns INTEGER, end_ns INTEGER);
"""


def default_db_path():
    base = os.environ.get("CROSSTRACE_DATA") or os.path.expanduser("~/.crosstrace-data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "crosstrace.db")


def connect(path=None):
    con = sqlite3.connect(path or default_db_path())
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_run(con, run, spans, truth):
    """Insert (or replace) one run with its spans and ground truth, atomically.

    run:   dict(run_id, fault_type, seed, split, started_ns, ended_ns, status, error)
    spans: list of span dicts as produced by agent.tracing.Tracing.spans()
    truth: dict(fault_type, target, params, start_ns, end_ns)
    """
    rid = run["run_id"]
    with con:
        for table in ("spans", "ground_truth", "runs"):
            con.execute(f"DELETE FROM {table} WHERE run_id = ?", (rid,))
        con.execute(
            "INSERT INTO runs VALUES (:run_id,:fault_type,:seed,:split,:started_ns,:ended_ns,:status,:error)",
            {"error": None, **run})
        con.executemany(
            "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?)",
            [(rid, s["span_id"], s["parent_id"], s["name"], s["start_ns"], s["end_ns"],
              s["attrs"].get("process.pid"), s["attrs"].get("thread.id"),
              json.dumps(s["attrs"], sort_keys=True)) for s in spans])
        con.execute(
            "INSERT INTO ground_truth VALUES (?,?,?,?,?,?)",
            (rid, truth["fault_type"], truth["target"], json.dumps(truth["params"], sort_keys=True),
             truth["start_ns"], truth["end_ns"]))


def run_exists(con, run_id):
    return con.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None


def list_runs(con, split, fault_type=None):
    q, args = "SELECT * FROM runs WHERE split = ?", [split]
    if fault_type:
        q += " AND fault_type = ?"
        args.append(fault_type)
    return [dict(r) for r in con.execute(q + " ORDER BY seed", args)]


def load_spans(con, run_id):
    rows = con.execute("SELECT * FROM spans WHERE run_id = ? ORDER BY start_ns, end_ns DESC", (run_id,))
    return [{"span_id": r["span_id"], "parent_id": r["parent_id"], "name": r["name"],
             "start_ns": r["start_ns"], "end_ns": r["end_ns"], "attrs": json.loads(r["attrs"])}
            for r in rows]


def load_truth(con, run_id):
    r = con.execute("SELECT * FROM ground_truth WHERE run_id = ?", (run_id,)).fetchone()
    return {"fault_type": r["fault_type"], "target": r["target"], "params": json.loads(r["params"]),
            "start_ns": r["start_ns"], "end_ns": r["end_ns"]}


# ---- Stage 2: kernel events, clock sync and run environment ----------------------------------

KERNEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_events (
    run_id TEXT NOT NULL, t_mono_ns INTEGER NOT NULL, ev TEXT NOT NULL, pid INTEGER, tid INTEGER,
    saddr TEXT, sport INTEGER, daddr TEXT, dport INTEGER, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS kernel_events_run ON kernel_events (run_id, t_mono_ns);
CREATE TABLE IF NOT EXISTS clock_sync (
    run_id TEXT NOT NULL, mono_ns INTEGER NOT NULL, wall_ns INTEGER NOT NULL, err_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS run_env (run_id TEXT PRIMARY KEY, info TEXT NOT NULL);
"""


def init_kernel_schema(con):
    con.executescript(KERNEL_SCHEMA)


def save_kernel(con, run_id, events, clock_sync, env):
    """events: collector dicts; clock_sync: [(mono_ns, wall_ns, err_ns)]; env: dict (ports, pids)."""
    init_kernel_schema(con)
    with con:
        for table in ("kernel_events", "clock_sync", "run_env"):
            con.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        con.executemany(
            "INSERT INTO kernel_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(run_id, e["t"], e["ev"], e.get("pid"), e.get("tid"), e.get("saddr"), e.get("sport"),
              e.get("daddr"), e.get("dport"),
              json.dumps({k: v for k, v in e.items() if k not in
                          ("t", "ev", "pid", "tid", "saddr", "sport", "daddr", "dport")}, sort_keys=True))
             for e in events])
        con.executemany("INSERT INTO clock_sync VALUES (?,?,?,?)", [(run_id, *c) for c in clock_sync])
        con.execute("INSERT INTO run_env VALUES (?,?)", (run_id, json.dumps(env, sort_keys=True)))


def load_kernel_events(con, run_id):
    init_kernel_schema(con)
    out = []
    for r in con.execute("SELECT * FROM kernel_events WHERE run_id = ? ORDER BY t_mono_ns", (run_id,)):
        e = {"t": r["t_mono_ns"], "ev": r["ev"], "pid": r["pid"], "tid": r["tid"], "saddr": r["saddr"],
             "sport": r["sport"], "daddr": r["daddr"], "dport": r["dport"], **json.loads(r["data"])}
        out.append({k: v for k, v in e.items() if v is not None})
    return out


def load_clock_sync(con, run_id):
    init_kernel_schema(con)
    return [tuple(r) for r in con.execute(
        "SELECT mono_ns, wall_ns, err_ns FROM clock_sync WHERE run_id = ? ORDER BY mono_ns", (run_id,))]


def load_env(con, run_id):
    init_kernel_schema(con)
    r = con.execute("SELECT info FROM run_env WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(r["info"]) if r else None
