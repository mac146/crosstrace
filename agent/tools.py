"""The agent's three tools: SQLite query, external HTTP API, configurable slow tool."""
import json
import sqlite3
import time
import urllib.request


def seed_db(path):
    """Create a small deterministic database."""
    con = sqlite3.connect(path)
    con.execute("DROP TABLE IF EXISTS orders")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)")
    con.executemany("INSERT INTO orders (customer_id, total) VALUES (?, ?)",
                    [(42, 10.5), (42, 20.0), (7, 99.0)])
    con.commit()
    con.close()


class Tools:
    def __init__(self, db_path, api_url, slow_extra_ms=0):
        self.db_path = db_path
        self.api_url = api_url
        self.slow_extra_ms = slow_extra_ms  # slow_tool fault hook

    def db_query(self, sql):
        con = sqlite3.connect(self.db_path)
        try:
            return json.dumps(con.execute(sql).fetchall())
        finally:
            con.close()

    def http_api(self, path):
        with urllib.request.urlopen(f"{self.api_url}{path}", timeout=30) as r:
            return r.read().decode()

    def slow_tool(self, work_ms):
        time.sleep((work_ms + self.slow_extra_ms) / 1000)
        return json.dumps({"done": True})

    def call(self, name, args):
        return getattr(self, name)(**args)
