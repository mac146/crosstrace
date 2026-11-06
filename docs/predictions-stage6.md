# Stage 6 predictions (written BEFORE any Stage 6 evaluation was run)

These are my expectations for the harder evaluations, committed first so they cannot be adjusted after seeing the
numbers. The results and where I was wrong are in `docs/results-stage6.md`.

State of the code when this was written: diagnosers `base` (spans only) and `v2` (the Stage 4 combined diagnoser with the
loss-explains-RTT rule). There is no `slow_api` rule yet. Everything below is about what these two do. The `v3`
diagnoser does not exist yet; it will be written only after the slowapi suite is read, and judged on a fresh seed set.

Setup common to all suites: kernel collector on (ports filtered, pure-ACK RTT samples dropped), 30 runs per fault
unless stated, healthy calibration runs separate from evaluation seeds, 95% Wilson intervals.

## Suite `slowapi` (normal magnitudes, seeds 3000+): the API server is slow, the network is healthy
* **base** (spans only, four-way split over dns_delay / net_latency / packet_loss / slow_api): slow_api top-1 about 25%
  (chance among four; anything from 10% to 45% is consistent), false attribution about 75% (it names the network
  three times out of four).
* **v2** cannot name a slow server at all: it has no such cause and falls back to a split over the three network
  causes. Predicted slow_api top-1 = **0%** and false attribution **at least 90%**. This is the case I flagged as the
  one I cannot be sure of; my expectation is that it fails, not that it handles it.
* On dns_delay / net_latency / packet_loss v2 should repeat its Stage 4 numbers (about 100% / 100% / 90%); base drops from
  about 30% to about 25% because its split now has four members.
* Healthy runs: no false alarms for either (coarse rule).

## Suite `subtle` (20-100 ms faults, seeds 5000+, 60 healthy runs)
* **Coarse threshold rule** (blind below roughly 50 ms): both diagnosers answer "none" for at least 90% of subtle faulty
  runs, so top-1 is near 0% for everything except by luck. Zero false alarms on healthy runs.
* **Tight rule** (p99 + 5 ms): detection becomes possible. I expect a non-zero false-alarm rate on the 60 healthy runs,
  somewhere between 3% and 30% (genuinely uncertain: depends on WSL jitter). For faults of 20-100 ms I expect the
  combined diagnoser to beat the baseline clearly on dns_delay and net_latency (the kernel measures them directly,
  the baseline is at chance), and to roughly tie on slow_tool and slow_db.
* I expect the faintest cases (a 20 ms fault) to be missed or misattributed more than the larger ones; I do not
  expect any rule setting to give both high detection and zero false alarms.

## Suite `pairs` (two faults at once, 8 pairs x 30, seeds 7000+; scored on finding BOTH in the top two)
* Pairs on different steps (dns_delay+slow_db, net_latency+slow_model, packet_loss+slow_tool, slow_api+slow_db,
  rate_limit+slow_db, slow_model+slow_tool): the combined diagnoser finds both in at least 80% of runs, except where a
  member is invisible by itself (packet_loss that never manifests, about 10% of runs). The baseline matches it on
  the two pairs with no network or server member (rate_limit+slow_db, slow_model+slow_tool), and is far worse (about
  25%) on pairs that contain a network or server fault, because that member sits in a four-way tie.
* Same-step pairs (dns_delay+packet_loss, dns_delay+slow_api): harder, because both causes act on one step. I expect
  60-80% for the combined diagnoser, lower than the different-step pairs.
* False attribution for the combined diagnoser: under 5%.

## What would make me wrong, stated in advance
* v2 handles slow_api well (it should not: it has no way to say it).
* The coarse rule detects a meaningful share of subtle faults.
* The tight rule has no false alarms at all on 60 healthy runs.
* Combined is no better than baseline on the network members of the pairs.

## Not covered, on purpose
packet_loss and rate_limit have no subtle variant (their delays come from TCP retransmit timers and a fixed 250 ms
backoff, so a 20-100 ms version does not exist). Three or more simultaneous faults, two faults that both need `tc
netem`, bursty or varying faults, and anything not in the fault catalogue are not tested.
