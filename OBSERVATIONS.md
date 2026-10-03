# Observations log

Dated notes on things found while setting up and testing the experiment,
before the full 5/20-minute measurement. This feeds directly into the paper's
Results and Limitations sections - update it as the real measurement runs.

## 2026-10-01 - Local trial (Windows, localhost)

- All latencies in `local_test.csv` landed around 2.1-2.2 seconds flat, for
  both variants and for cold and warm requests alike.
- Cause: Windows resolves the hostname `localhost` by first trying IPv6
  (`::1`), which times out before falling back to IPv4 (`127.0.0.1`). This
  adds a fixed ~2s delay to every request, unrelated to the application.
- Takeaway: the local trial only validates that the scripts work end-to-end;
  it does not produce usable latency numbers. Do not use `local_test.csv`
  values in the report. (Using `http://127.0.0.1:PORT` instead of `localhost`
  avoids this, if a clean local number is ever needed.)

## 2026-10-01 - Cloud quick test (`--idle 0.1min`, ~6s gaps)

- The same `instance_id` appeared in every row for both services throughout
  the quick test (`b9f759a9` for scale-zero, `f303757c` for always-warm).
  Neither instance was actually torn down and restarted during the test.
- 6 seconds of idle time is far too short to trigger Cloud Run's
  scale-to-zero teardown; this is expected, not a bug. The full measurement
  uses 5- and 20-minute idle periods specifically because they are long
  enough to matter - watch whether `instance_id` changes between calls in
  that data; that is the real signal for a teardown/cold start, in addition
  to the `cold` flag.
- The always-warm service showed latency spikes of ~5.2s on most requests
  (vs ~200-340ms for scale-zero), even though `cold=False` and the instance
  never restarted.
  - Likely cause: Cloud Run's default CPU allocation setting ("CPU is only
    allocated during request processing") throttles a container's CPU down
    when it isn't actively handling a request - even for a min-instances=1
    service. The first request after a gap has to wait for the CPU to be
    "woken up" again, producing a cold-start-like delay despite the
    container having run continuously the whole time.
  - This is a different mechanism from the scale-to-zero cold start the
    abstract is about, but it's a relevant nuance: a "warm" instance under
    Cloud Run's default settings is not automatically free of latency
    spikes. Worth a paragraph in Discussion/Limitations. Optional follow-up:
    redeploy the warm variant with `gcloud run deploy --no-cpu-throttling`
    (CPU always allocated) and see whether the spikes disappear - would be
    a clean, citable extra result if time allows.
- Conclusion: the measurement pipeline (`measure.py`, `analyze.py`) is
  confirmed working end-to-end against real Cloud Run URLs. Proceeding to
  the full 5/20-minute measurement.

## Open questions for the full run

- At what idle time does scale-zero actually get torn down (`instance_id`
  should change, `cold` should flip to `True`)? Cloud Run doesn't publish an
  exact number; the 5 vs 20 minute comparison is partly designed to find out
  empirically.
- Does the always-warm CPU-throttling spike persist with longer idle gaps,
  or was it specific to the very short 6-second gaps in the quick test?
- How do the `sequential` (back-to-back) numbers compare once both instances
  have had real, sustained traffic, rather than the paired single requests
  used in the quick test?

## 2026-10-02/03 - Full overnight measurement (results.csv, 265 rows)

Idle tiers used: 5min x10, 15min x10, 30min x8, 60min x3, then 100 sequential
requests. Total runtime ~9 hours (18:12 UTC to ~04:33 UTC), e2-micro VM in
europe-central2, same region as the Cloud Run services.

Cold start rate per idle tier (scale-zero only; always-warm never showed a
cold start after the initial deploy):
- 5 min idle:  1/10 cold (only the very first request after deploy)
- 15 min idle: 0/10 cold (instance stayed warm throughout)
- 30 min idle: 8/8  cold (torn down and restarted EVERY time)
- 60 min idle: 3/3  cold (same, every time)

Conclusion: for this configuration (1 vCPU, 1 GiB, this FastAPI app,
europe-central2), Cloud Run's scale-to-zero teardown threshold sits
somewhere between 15 and 30 minutes of idleness. Below it, an instance
reliably stays warm; at or above it, teardown is consistent (100% in both
the 30 and 60 minute tiers).

Cold start latency: 3.2-5.9s (vs ~200-350ms when warm) - roughly a
15-20x slowdown on the first request after teardown.

Billing validation (Group by SKU, see TEST1_REPORT.md section 6): over the
~11.35h the always-warm service existed during this window, it billed
€0.26 total (€0.13 CPU + €0.13 memory, SKU "Min Instance ... Tier 2");
scale-zero billed effectively €0.00 (SKU "... Tier 2 (Request-based
billing)", 79s/79 GiB*s of actual processing). Real rate derived from this:
€0.26 / 40,854.6 s ≈ €0.00000636/s (~€0.023/h) for the always-warm service
existing (1 vCPU + 1 GiB combined, continuous, Tier 2 region pricing).

## 2026-10-03 - Test 2 plan and the idle-gap billing correction

Test 2 design (see README.md section 3 and TEST2 run below): drop the 5-min
tier (nothing happens there), keep 15min (expect ~0% cold) and 30min
(expect 100% cold) as the two "obvious" ends, and add 20min + 25min *inside*
the 15-30min window specifically to capture a mixed cold-start rate with a
usable 95% CI (Wilson score, now computed by analyze.py) - i.e. to trace how
the rate actually rises across the boundary instead of treating it as one
step. All four tiers run with 10 repetitions each (`--tier 15:10 --tier
20:10 --tier 25:10 --tier 30:10 --sequential 100`), ~15h estimated total.

Unlike Test 1 (which started right after redeploy), the always-warm service
kept running continuously between Test 1's end and Test 2's start - it does
not stop just because no measurement is active. This means the raw billing
*delta* between "end of Test 1" and "end of Test 2" will include three
components, not just Test 2:

  idle gap (Test 1 end -> Test 2 start) + Test 2's own ~15h window + whatever
  gap follows until the next billing check

Because Cloud Run's min-instance billing is a flat rate per elapsed wall-
clock second (not request-based - it doesn't matter whether the instance is
idle or actually serving a request), this is NOT a problem: the idle-gap
portion can be subtracted out arithmetically using the real per-second rate
already derived from Test 1 above, rather than needing hour-level filtering
in the Billing Reports UI (which only supports day-level granularity anyway).

Known timestamps so far:
- Test 1 end: ~2026-10-03 04:33 UTC
- Test 2 start: ~2026-10-03 18:54 UTC (PID 1434 on coldstart-measurer)
- Idle gap: ~14h 21m =~ 51,660 s
- Test 2 end: TBD - read the actual start/`Done ->` timestamps from
  measure2.log once it finishes, rather than relying on the estimate.

Formula to apply once Test 2's billing delta is visible (usually ~24h after
the window closes):

  predicted_idle_gap_cost  = idle_gap_seconds  x 0.00000636 EUR/s
  predicted_test2_cost     = test2_duration_s  x 0.00000636 EUR/s
  predicted_total_delta    = predicted_idle_gap_cost + predicted_test2_cost

Compare predicted_total_delta against the actual Min-Instance-seconds delta
shown in Billing -> Reports -> Group by SKU (current baseline snapshot,
right at Test 1's end state: 40,854.6 s / 79 s / 81 count - confirmed via
screenshot on 2026-10-03). If they match (within rounding), subtracting
predicted_idle_gap_cost from the real total delta gives a clean, isolated
Test-2-only cost figure, safe to report alongside Test 1's €0.26 number.
scale-zero needs no such correction - it is request-based, so it only bills
during the two measurement windows themselves, never during the gap between
them.