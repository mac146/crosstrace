## Technical report v1

A pre-registered, open benchmark for diagnosing slow AI-agent runs by joining agent traces with kernel-level (eBPF) network evidence, with the negative results kept in.

**Read:** [docs/report/report.md](https://github.com/mac146/crosstrace/blob/report-v1/docs/report/report.md)

**Headline results (all on mock services unless stated; details and limits in the report):**
- Kernel evidence raises top-1 accuracy on network-path faults from about 30% to about 97% (fresh set of 90 runs).
- The per-span join matched ground truth in 160/160 runs and held up to 512 simultaneous agent threads.
- The real OpenTelemetry eBPF agent (OBI v1.0.0) gives equal diagnostic accuracy once each call is matched exactly to its connection; matched by connection order under concurrency it falls to 63-69%.
- On a real model over the internet: DNS delay found in 10/10 and latency in 9/10 runs (10 runs per group, one path); packet loss not separable from background retransmits.

**Not claimed:** production deployment, HTTP/3, streamed responses, multi-host systems, packet loss on real paths. The collector needs root-equivalent access: development machines only.

**Pre-registered follow-ups (not yet run):** `docs/predictions-stage17.md`, `docs/predictions-stage18.md`.

Predictions for every stage from 6 on were committed before their runs: `docs/predictions-stage*.md`.
