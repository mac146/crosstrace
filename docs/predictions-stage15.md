# Stage 15 predictions: the real OpenTelemetry eBPF Instrumentation (OBI) against our join (written BEFORE it was ever run)

Binary: OBI v1.0.0 linux-amd64 from the official release. The release tarball's SHA-256 matched the published SHA256SUMS; the extracted
binary's SHA-256 is b239d223...4462 (pinned in scripts/setup_sudoers.sh, which installs it only on an exact match). The Sigstore signature was
NOT cryptographically verified (no cosign here); the bundle's certificate names the project's release workflow at tag v1.0.0. It runs as root,
on the dev WSL only, against our mock services only.

What is compared: the Stage 14 scenarios (sel_slow_api, sel_latency, two_servers, mixed_one_server; same seeds, same calibration), scoring with
the same rules as v3. New arms, built from what real OBI emits while the scenario runs:
- `obi_real`      per-request server duration from OBI's HTTP spans for each agent call (matched by request path and time), nothing else.
- `obi_real+net`  as above plus OBI's network metrics (TCP RTT, retransmit counters) as host-wide numbers, if OBI exposes them in a form we can read.
The emulated `obi`, `pool`, `pool_port` and `v3` arms are recomputed on the same runs for a fair side by side.

Predictions:
1. OBI starts and reports HTTP spans for the mock API server and the agent's client calls on this WSL kernel (6.6.87.2): 65%. If it does not,
   the stage reports that and the arm stays emulated.
2. If it runs: OBI's per-request server duration is within max(10%, 30 ms) of our own server-wait for at least 90% of the slow calls (70%).
   Hence `obi_real` matches the emulated `obi` on sel_slow_api (at least 90% top-1).
3. `obi_real` alone names no network cause: at most 30% top-1 on sel_latency (80%).
4. `obi_real+net`, using host-wide metrics, behaves like `pool`: at least 90% on sel_latency, at most 30% "both in top 2" on mixed_one_server (65%;
   the weakest prediction, because I do not know what OBI's network metrics expose per flow or per destination).
5. Neither real-OBI arm beats `v3` in any scenario (90%), and `v3` is at least 80 points ahead on mixed_one_server (75%).
6. Healthy runs: no real-OBI arm false-alarms on more than 1 of 20 (85%).

I would be wrong, and say so, if OBI's per-request duration differs from our server-wait by more than 30% (then the Stage 14 emulation misstated
OBI), or if `obi_real+net` matches `v3` on mixed_one_server (then OBI already carries per-call network evidence that the join was supposed to add).
