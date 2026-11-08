"""The agent's three tools: SQLite query, external HTTP API, configurable slow tool."""
import http.client
import json
import sqlite3
import time
from urllib.parse import urlparse

from agent.dns import resolve


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
    def __init__(self, db_path, api_url, dns_server, tracing, slow_extra_ms=0, db_hook=None):
        self.db_path = db_path
        self.api_url = api_url
        self.dns_server = dns_server
        self.tracing = tracing
        self.slow_extra_ms = slow_extra_ms  # slow_tool fault hook
        self.db_hook = db_hook              # slow_db fault hook: called right before the first query connects

    def db_query(self, sql):
        attrs = {"db.system.name": "sqlite", "db.query.text": sql}
        with self.tracing.span("SELECT orders", attrs, client=True):
            if self.db_hook:
                self.db_hook()
            con = sqlite3.connect(self.db_path, timeout=10)  # waits on a locked db
            try:
                return json.dumps(con.execute(sql).fetchall())
            finally:
                con.close()

    def http_api(self, path):
        u = urlparse(self.api_url)
        attrs = {"http.request.method": "GET", "server.address": u.hostname,
                 "server.port": u.port, "url.full": f"{self.api_url}{path}"}
        with self.tracing.span("GET", attrs, client=True) as span:
            # DNS lookup, TCP connect and the request all happen inside this one span:
            # an app-level tracer has no separate view of them.
            ip = resolve(u.hostname, self.dns_server)
            conn = http.client.HTTPConnection(ip, u.port, timeout=30)
            try:
                conn.request("GET", path, headers={"Host": u.hostname})
                # validation-only ground truth for the Stage 3 join test; the correlator never reads it.
                # Must be read here: getresponse() detaches the socket when the server will close it.
                span.set_attribute("crosstrace.debug.local_port", conn.sock.getsockname()[1])
                resp = conn.getresponse()
                span.set_attribute("http.response.status_code", resp.status)
                return resp.read().decode()
            finally:
                conn.close()

    def slow_tool(self, work_ms):
        time.sleep((work_ms + self.slow_extra_ms) / 1000)
        return json.dumps({"done": True})

    def call(self, name, args):
        return getattr(self, name)(**args)
