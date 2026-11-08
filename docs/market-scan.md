# Market scan (2026-11-08): who needs cross-layer agent diagnosis, and who pays

A first pass of web searches, NOT a systematic study. Reliability is marked per item: **P** = primary or well-sourced, **S** = secondary
(vendor blog or aggregator quoting a report), **U** = unsourced headline (not used). Nothing here comes from customers.

## What the searches support
- Agent observability is a funded category, but the money goes to tracing, evaluation and cost, not to kernel-level evidence. (S) Reported
  rounds: Langfuse $50M Series B (Mar 2026), Braintrust $80M Series B (Feb 2026), Coralogix $200M Series F, smaller seeds for Laminar,
  Raindrop, Respan; one aggregator tracks about 36 such startups with about $1.1B funding. Figures come from aggregators and blogs.
- Practitioners already trace: in LangChain's State of Agent Engineering survey (1,340 respondents), 94% of production agents have some
  observability and 71.5% trace individual steps and tool calls; far fewer run evaluations (offline 52.4%, online 37.3%). (S, via vendor blogs)
- A dominant failure cause in the one dataset I found is provider-side, not network: Datadog's State of AI Engineering 2026 reportedly puts
  rate-limit errors at nearly a third of LLM call failures in March 2026. (S) This fits the guess that DNS/packet loss between your own services
  is a small share of agent incidents; it is NOT a measurement of that share.
- The "basic metrics stack" baseline is real and free: OpenTelemetry eBPF Instrumentation (OBI) already provides TCP RTT, retransmit and
  socket I/O network metrics plus per-request application spans, kernel 5.8+ (4.18 on RHEL-family). (P, OpenTelemetry docs)
- eBPF at the kernel/TCP level cannot see QUIC/HTTP3 (no in-kernel QUIC), and stream-level HTTP/2 or TLS content needs user-space probes
  (uprobes on TLS libraries) or in-process instrumentation. (S/P mix: a 2026 arXiv paper, a Linux Plumbers talk)

## What I did NOT find
- Any figure for what share of agent incidents are network or kernel-level, or how long they take to diagnose.
- Latency-specific incident data; Datadog Universal Service Monitoring or Cilium Hubble comparisons; pricing for LangSmith, Arize or Datadog LLM
  observability; any customer who pays for cross-layer agent diagnosis; any prior work scoring such diagnosis (also see results-stage13.md).
- Unsourced headlines ("88% of enterprise agents fail", "78% had a customer-facing incident") were not used.

## What it changes in the plan
1. The decisive unknown is still prevalence in real incidents; the scan gives a hint (provider-side causes dominate) but no number.
2. Our collector overlaps what OBI ships. A credible product shape is a diagnoser/benchmark that CONSUMES OBI-style network metrics and spans
   rather than a competing collector. The next experiment should use the real OBI binary, not an emulation.
3. Protocol coverage (HTTP/2 streaming, HTTP/3) is a known blind spot of kernel-level TCP probes and should be tested before any claim about real
   model providers.
