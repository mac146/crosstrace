"""HTTP/1.1 on a raw asyncio stream, with full control of the bytes and the socket.

A web framework would hide exactly what the fault injector needs: writing half a chunk, ending a chunked body early, holding a
connection silent, closing with FIN or with RST. So requests are parsed and responses written here, by hand.
"""
import asyncio
import socket
import struct
import sys
from dataclasses import dataclass, field

REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 408: "Request Timeout", 413: "Payload Too Large",
           429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
           529: "Overloaded"}
# never copied between hops: they describe one connection, not the message
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding", "te", "trailer", "upgrade",
               "proxy-connection", "proxy-authenticate", "proxy-authorization"}


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: list = field(default_factory=list)       # [(name, value)] in arrival order
    body: bytes = b""

    def header(self, name, default=None):
        name = name.lower()
        for k, v in self.headers:
            if k.lower() == name:
                return v
        return default

    @property
    def path(self):
        return self.target.split("?", 1)[0]

    @property
    def keep_alive(self):
        conn = (self.header("connection") or "").lower()
        if self.version == "HTTP/1.0":
            return conn == "keep-alive"
        return conn != "close"


class BadRequest(Exception):
    pass


async def _read_chunked(reader):
    out = bytearray()
    while True:
        size_line = await reader.readline()
        if not size_line:
            raise asyncio.IncompleteReadError(bytes(out), None)
        size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):   # trailers
                pass
            return bytes(out)
        out += await reader.readexactly(size)
        await reader.readexactly(2)


async def read_request(reader):
    """Next request on a keep-alive connection, or None when the client closed it."""
    while True:
        line = await reader.readline()
        if not line:
            return None
        if line.strip():
            break                                       # tolerate stray CRLF between requests
    try:
        method, target, version = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    except ValueError:
        raise BadRequest(f"bad request line: {line[:80]!r}")
    headers = []
    while True:
        h = await reader.readline()
        if h in (b"\r\n", b"\n", b""):
            break
        if b":" not in h:
            raise BadRequest(f"bad header line: {h[:80]!r}")
        k, v = h.decode("latin-1").split(":", 1)
        headers.append((k.strip(), v.strip()))
    req = Request(method, target, version, headers)
    if (req.header("transfer-encoding") or "").lower() == "chunked":
        req.body = await _read_chunked(reader)
    else:
        n = int(req.header("content-length") or 0)
        req.body = await reader.readexactly(n) if n else b""
    return req


class ResponseWriter:
    """Writes responses on one client connection. The low-level methods (chunk, raw, close_fin, reset) are what faults use."""

    def __init__(self, writer):
        self.w = writer

    @staticmethod
    def _head(status, headers):
        lines = [f"HTTP/1.1 {status} {REASONS.get(status, 'Status')}"] + [f"{k}: {v}" for k, v in headers]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    def _end_to_end(headers):
        return [(k, v) for k, v in headers if k.lower() not in HOP_HEADERS]

    async def send_full(self, status, headers, body, close=False):
        hs = self._end_to_end(headers) + [("Content-Length", str(len(body)))] + ([("Connection", "close")] if close else [])
        self.w.write(self._head(status, hs) + body)
        await self.w.drain()

    async def start_chunked(self, status, headers):
        self.w.write(self._head(status, self._end_to_end(headers) + [("Transfer-Encoding", "chunked")]))
        await self.w.drain()

    async def chunk(self, data):
        if data:
            self.w.write(b"%x\r\n%s\r\n" % (len(data), data))
            await self.w.drain()

    async def end_chunked(self):
        self.w.write(b"0\r\n\r\n")
        await self.w.drain()

    async def raw(self, data):
        """Arbitrary bytes, e.g. half a chunk."""
        self.w.write(data)
        await self.w.drain()

    async def close_fin(self):
        """Orderly close: the client sees EOF (FIN)."""
        self.w.close()
        try:
            await self.w.wait_closed()
        except ConnectionError:
            pass

    def reset(self):
        """Abortive close: SO_LINGER 0 makes the kernel send RST instead of FIN."""
        sock = self.w.get_extra_info("socket")
        if sock is not None:
            fmt = "HH" if sys.platform == "win32" else "ii"
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack(fmt, 1, 0))
        self.w.transport.abort()
