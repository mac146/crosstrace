## Technical report v2

A pre-registered, open benchmark for diagnosing slow AI-agent runs by joining agent traces with kernel-level (eBPF) network evidence, with the negative results kept in. v2 restructures the report and adds Stages 15-19.

**Read:** [docs/report/report.md](https://github.com/mac146/crosstrace/blob/report-v2/docs/report/report.md)  ·  **Short blog version:** https://dev.to/032_mayank_d7cf900fa03d4b/i-tested-whether-ebpf-helps-diagnose-slow-ai-agents-i-wrote-my-predictions-first-and-got-many-f8h

**What is new since v1**
- **Real OpenTelemetry eBPF agent (OBI v1.0.0) compared with our join** (Stages 15-16): it names a slow server as well as the join, and equals the join once each call is matched exactly to its connection (99.5-100% with 9 and 24 concurrent agents in different conditions); matched by connection order it scores 63-82%.
- **Async agents without a declared port** (Stages 17 and 19): assigning calls to connections by start time and duration reaches 99.5-100% when calls start in order, but falls 4, 17 and 21 points behind a declared port at random waits of 50, 200 and 500 ms before connecting. Pre-registered label: partial.
- **8 concurrent real agents on a real model and network path** (Stage 18): DNS delay detected in 100% and latency in 94% of agent-runs, 0/64 healthy false alarms, no model errors, about USD 0.02.
- 17 negative or wrong-prediction results are listed in the report (Section 5).

**Not claimed:** production deployment, HTTP/3, streamed responses, multi-host systems, packet loss on real paths, how often network causes occur in real agent incidents. The collector needs root-equivalent access: development machines only.

**Pre-registration:** predictions for every stage from 6 on were committed before their runs (`docs/predictions-stage*.md`); one known deviation (a plumbing smoke test in Stage 19) is disclosed in `docs/results-stage19.md`.
