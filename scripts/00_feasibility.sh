#!/usr/bin/env bash
# Stage 0 / task 1: can this WSL kernel do eBPF tracing and tc netem?
# REQUIRES ROOT (sudo). Temporarily modifies loopback qdisc (reverted at end).
# Run only inside the dev WSL distro.
set -u

echo "== installing tools =="
sudo apt-get update -qq
sudo apt-get install -y -qq bpftrace iproute2 curl || echo "apt install had problems (continuing)"

echo "== bpftrace: trace one connect() syscall =="
sudo timeout 10 bpftrace -e 'tracepoint:syscalls:sys_enter_connect { printf("connect pid=%d comm=%s\n", pid, comm); exit(); }' &
BPF=$!
sleep 3
curl -s -m 2 http://127.0.0.1:9 >/dev/null
if wait "$BPF"; then echo "BPFTRACE: OK"; else echo "BPFTRACE: FAILED"; fi

echo "== netem on lo =="
sudo modprobe sch_netem 2>&1 || true
if lsmod | grep -q sch_netem; then echo "module: loaded"; else echo "module: NOT loaded"; fi
if sudo tc qdisc add dev lo root netem delay 10ms loss 1%; then
  tc qdisc show dev lo
  echo "NETEM: OK"
  sudo tc qdisc del dev lo root
else
  echo "NETEM: FAILED"
fi
