"""Shared by the fault-table client programs: print exactly one JSON line, whatever happens."""
import json
import sys


def run(fn):
    try:
        out = fn()
        print(json.dumps({"ok": True, "out": out}, default=str))
    except BaseException as e:            # noqa: BLE001 - the point is to report what the SDK raised
        print(json.dumps({"ok": False, "error": type(e).__name__, "msg": str(e)[:200]}))
    sys.stdout.flush()
