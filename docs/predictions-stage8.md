## Stage 8 predictions: faults on the REAL network path to a real model (written BEFORE the run)

Setup: LangGraph + `z-ai/glm-5.3-flash` via OpenRouter; a fresh HTTP client per run, so each run pays a DNS lookup and a TLS
connect. Faults (faultlab/real_path.py): `dns_delay` = UDP to the resolver delayed 4-8 s; `net_latency` = egress to the model
server delayed 0.8-1.5 s per packet; `packet_loss` = egress to the model server loses 15-30% of packets. 8 runs per group
plus 8 healthy runs; thresholds from 16 NEW healthy runs recorded with the new probes (libc DNS, tcp_send). Diagnosers base,
v3, v4 (all frozen). n = 8 per group: observations, not accuracy estimates.

Probes (kernel side), in advance:
1. **libc DNS probe.** The healthy first model call of at least 90% of fresh-connection runs has a captured lookup of the
   model host (smoke run: 144 ms). In dns_delay runs where the fault was applied, the measured lookup is within 15% of the
   injected delay (the resolver is asked twice, A and AAAA, in parallel).
2. **Reused connections.** At least 90% of model-call spans carry some kernel evidence (before the send probe: connect on 1%).
3. **Remote server wait** is defined (not "-") for at least 90% of model-call spans, and in healthy runs tracks the span
   duration minus lookup/connect to within 20%.
4. **net_latency**: the measured max smoothed RTT on the model connection is well above the healthy baseline (above 300 ms in
   at least 6 of 8 runs; healthy is 10-100 ms), i.e. the kernel sees the injected delay.
5. **packet_loss**: retransmits per run in the loss group exceed the healthy baseline in at least 6 of 8 runs.

Faults are on the path: the "applied" flag is true in at least 90% of net_latency and packet_loss runs (the CDN's addresses
may change between my resolving them and the connection) and 100% of dns_delay runs.

Diagnosers, in advance, and this is where I expect to be wrong:
6. **Spans only blames the model.** The slow step is the model call, so base answers `slow_model` for nearly every fault run:
   top-1 at most 25% on all three real network faults and false attribution at least 70% (it accuses a healthy model).
7. **v4 does better than base but badly.** Its model-call rule (the `chat` step) was only ever exercised on faults on the
   API-tool path; on the model connection I expect: `dns_delay` at most 50% (the rule only looks at DNS inside the
   network-evidence branch), `net_latency` at most 50% (background retransmits of a real network send it to `packet_loss`),
   `packet_loss` at least 60%. I expect to need a version 5 whose model-call step uses the same DNS / connect / RTT /
   retransmit attribution as other HTTP steps, with thresholds calibrated per span class (a remote model call's lookup is
   about 150 ms and its wait seconds; the local API tool's are 1 ms).
8. **Healthy runs**: v4 false alarms at most 25%, spans only 0%.
9. Some runs hit the 60 s client timeout under 30% loss and are scored as errors.

I would be wrong on 6 if spans-only gets at least 40% on any real fault; on 7 if v4 reaches at least 80% on both dns_delay
and net_latency.

Not covered (stated in advance): a slow remote SERVER cannot be injected (the model is someone else's); that signal is only
observed on healthy calls. Real-network faults are injected as egress-only, one-way effects.
