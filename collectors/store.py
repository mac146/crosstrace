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
