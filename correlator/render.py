"""Human-readable view of a Timeline: spans as a tree, kernel evidence under each HTTP span."""
from correlator.join import breakdown


def _fmt(x):
    return "-" if x is None else f"{x:.1f}ms"


def _event_line(a, t0, verbose):
    e = a.event
    at = f"+{(e['wall_ns'] - t0) / 1e6:9.1f}ms"
    kind = e["ev"]
    if kind == "dns_query":
        what = (f"DNS lookup of {e['host']} (libc)" if e.get("via") == "libc"
                else f"DNS query -> {e['daddr']}:{e['dport']}")
    elif kind == "dns_response":
        what = f"DNS {'lookup done' if e.get('via') == 'libc' else 'response'} after {e['lat_ns'] / 1e6:.1f}ms"
    elif kind == "tcp_connect_start":
        what = f"TCP connect start -> :{e['dport']}"
    elif kind == "tcp_connect_end":
        what = f"TCP connected after {e['lat_ns'] / 1e6:.1f}ms (client port {e['sport']})"
    elif kind == "tcp_retransmit":
        what = f"TCP RETRANSMIT {e['sport']}->{e['dport']} (state {e['state']})"
    elif kind == "tcp_send":
        what = f"TCP send {e['sport']}->{e['dport']} {e['bytes']} bytes"
    else:
        what = f"rtt {e['sport']}->{e['dport']} srtt={e['srtt_us']}us bytes={e['bytes']}"
    return f"{'  ' * 0}    | {at}  {what}   [{a.match}]"


def render(timeline, verbose=False):
    t0 = timeline.t0_ns
    out = [f"clock alignment error bound: {timeline.clock_err_ns / 1000:.1f} us"]
    if timeline.clock_max_jump_ns > 1_000_000:
        out.append(f"WARNING: wall clock stepped {timeline.clock_max_jump_ns / 1e6:.1f} ms against the monotonic clock "
                   f"during this run; events near that moment may be attached to a neighbouring span")
    for entry in timeline.entries:
        s = entry.span
        pad = "  " * entry.depth
        dur = (s["end_ns"] - s["start_ns"]) / 1e6
        out.append(f"{pad}{s['name']:<28} +{(s['start_ns'] - t0) / 1e6:9.1f}ms {dur:9.1f}ms")
        if not entry.kernel:
            continue
        for a in entry.kernel:
            if a.event["ev"] == "tcp_rtt" and not verbose:
                continue
            out.append(pad + _event_line(a, t0, verbose))
        b = breakdown(entry)
        n_rtt = sum(a.event["ev"] == "tcp_rtt" for a in entry.kernel)
        out.append(f"{pad}    = evidence: dns {_fmt(b['dns_ms'])}, connect {_fmt(b['connect_ms'])}, "
                   f"retransmits {b['retransmits']}, server wait {_fmt(b['server_wait_ms'])}, "
                   f"unexplained {_fmt(b['unexplained_ms'])}   ({n_rtt} rtt samples, max srtt {_fmt(b['srtt_max_ms'])})")
    if timeline.orphans:
        out.append(f"\n{len(timeline.orphans)} kernel events not attached to any span "
                   f"(other loopback traffic or events outside every span window)")
    return "\n".join(out)
