"""Minimal DNS A-record client and codec (stdlib only).

The agent resolves the API hostname by sending a real UDP query to a configurable resolver,
so a slow resolver (the dns_delay fault) shows up as real packets on the wire for the
Stage 2 eBPF collector. No caching, so every lookup pays the resolver latency.
Limit: this bypasses libc's resolver; see docs/decisions.md D9.
"""
import random
import socket
import struct


def build_query(name, qid):
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split("."))
    return struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack("!HH", 1, 1)


def _question_end(data):
    i = 12
    while data[i] != 0:
        i += data[i] + 1
    return i + 1 + 4


def build_response(query, ip):
    """Answer any A query with `ip`."""
    qid = struct.unpack("!H", query[:2])[0]
    question = query[12:_question_end(query)]
    header = struct.pack("!HHHHHH", qid, 0x8180, 1, 1, 0, 0)
    answer = struct.pack("!HHHIH", 0xC00C, 1, 1, 0, 4) + socket.inet_aton(ip)
    return header + question + answer


def _skip_name(data, i):
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def parse_a_response(data):
    """Return the list of IPv4 addresses in an A response."""
    _, _, qd, an, _, _ = struct.unpack("!HHHHHH", data[:12])
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _, _, rdlen = struct.unpack("!HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return ips


def resolve(name, server, timeout=10.0):
    qid = random.getrandbits(16)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(build_query(name, qid), server)
        data, _ = s.recvfrom(512)
    ips = parse_a_response(data)
    if not ips:
        raise OSError(f"no A record for {name}")
    return ips[0]
