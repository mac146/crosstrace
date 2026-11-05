"""Self-checks on a joined run. These test the join against facts that do not depend on the matching
algorithm's own choices: the span's declared target and the kernel's exact PID/TID and ports.

Returns a list of human-readable problems (empty = all good).
"""
from correlator.join import _is_hostname, _is_http


def check(timeline, env):
    problems = []
    tracked = {env["api_port"], env["llm_port"]}
    http = [e for e in timeline.entries if _is_http(e.span)]

    for e in http:
        a = e.span["attrs"]
        label = f"{e.span['name']}@{a['server.port']}"
        kinds = [x.event["ev"] for x in e.kernel]
        for need in ("tcp_connect_start", "tcp_connect_end"):
            if kinds.count(need) != 1:
                problems.append(f"{label}: expected exactly 1 {need}, got {kinds.count(need)}")
        looked_up = _is_hostname(e.span)
        if looked_up != (kinds.count("dns_response") >= 1):
            problems.append(f"{label}: hostname lookup={looked_up} but dns_response count={kinds.count('dns_response')}")
        # Independent ground truth: the agent recorded the real client port of the socket it used.
        # The join never reads it, so agreement is evidence the join found the right connection.
        truth_port = a.get("crosstrace.debug.local_port")
        if truth_port is not None:
            ports = {x.event["sport"] for x in e.kernel if x.event["ev"] == "tcp_connect_end"}
            if ports != {truth_port}:
                problems.append(f"{label}: joined connection client port(s) {sorted(ports)} != real {truth_port}")
            for x in e.kernel:
                if x.event["ev"] in ("tcp_rtt", "tcp_retransmit") and truth_port not in (
                        x.event.get("sport"), x.event.get("dport")):
                    problems.append(f"{label}: {x.event['ev']} from a different connection was attached")
        for x in e.kernel:
            ev = x.event
            if ev["ev"] in ("dns_query", "dns_response"):
                continue
            if a["server.port"] not in (ev.get("sport"), ev.get("dport")):
                problems.append(f"{label}: attached {ev['ev']} does not touch the span's port")
            if ev["ev"] in ("tcp_connect_start", "tcp_connect_end"):
                t = ev["wall_ns"]
                slack = 1_000_000 + timeline.clock_err_ns
                if not (e.span["start_ns"] - slack <= t <= e.span["end_ns"] + slack):
                    problems.append(f"{label}: {ev['ev']} outside the span window")

    # Client ports our own agent opened to a service: only flows with such a port are "ours". Other
    # processes' traffic can touch a service port number by chance (ports are random per run).
    all_events = [x.event for e in timeline.entries for x in e.kernel] + [ev for ev, _ in timeline.orphans]
    our_client_ports = {ev["sport"] for ev in all_events
                        if ev["ev"] == "tcp_connect_end" and ev.get("dport") in tracked}

    # nothing of ours may be left unattached
    for ev, _ in timeline.orphans:
        if ev["ev"] in ("dns_query", "dns_response") or (
                ev["ev"] in ("tcp_connect_start", "tcp_connect_end") and ev.get("dport") in tracked):
            # connects to other ports are other processes on the machine: not ours to explain
            problems.append(f"orphan {ev['ev']} (pid-pinned events of our services must always attach)")
        elif ev["ev"] in ("tcp_retransmit", "tcp_rtt") and tracked & {ev.get("sport"), ev.get("dport")}                 and our_client_ports & {ev.get("sport"), ev.get("dport")}:
            problems.append(f"orphan {ev['ev']} on a service port {ev.get('sport')}->{ev.get('dport')}")
    return problems
