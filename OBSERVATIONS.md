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

## 2026-10-12 - Test 2 result and what it changes for Test 3

Test 2 (results_2.csv): scale-zero cold starts 15 min 1/10, 20 min 10/10,
25 min 10/10, 30 min 10/10. The boundary is between 15 and 20 minutes, not
between 25 and 30.

The one cold sample at 15 min is `idle_15min` iteration 1 = the first request
after the ~14 h idle gap between Test 1 and Test 2, i.e. the instance was gone
for a reason unrelated to the 15-minute idle time (`instance_uptime_s` 2.55).
Iterations 2-10 are all warm with `instance_uptime_s` growing in 900 s steps.
Excluding it: 15 min = 0/9 in Test 2, 0/19 over Test 1 + Test 2. Same artifact
in Test 1: the single cold `idle_5min` sample is iteration 1 (corrected 0/9).
The Wilson CIs printed by the old analyze.py for these tiers include the
artifact; use `analyze3.py --legacy-drop-first` for the corrected numbers.
Fix in Test 3: every service gets a warm-up request first, so no measured
sample can be "first after a long gap".

Test 3 design decisions: one service per tier run in parallel (same night for
all tiers, total time = longest tier), tiers 15-20 min in 1 min steps, 20
samples each, real idle time logged, IPv4-only timing split, BigQuery billing
export with per-service labels, teardown time from Cloud Logging (needs a
pilot to confirm the "Shutting down" line is written on SIGTERM).

## 2026-10-16 - Test 3 result, what did not work

Run 2026-10-12, 12:22-19:05 UTC. 15 min: 1/20 cold; 16/17/18/19/20 min:
14/14, 17/17, 10/10, 10/10, 13/13 cold. Boundary = crossing between 15 and 16
minutes. SIGTERM ("Shutting down") 888.6-910.1 s after the last response in 64
of 65 cold samples (median 903.3 s). Cold median 3375 ms vs warm 251 ms.

Findings that are NOT explained by the idle timeout (no cause claimed):
- 36 of 120 attempts got HTTP 429 "The request was aborted because there was
  no available instance" (httpRequest.latency 0 s in the log, 10-23 s waited
  by the client), only at the tiers 16-20, never at 15, never in Tests 1/2.
  The services had max-instances=1; not tested whether that matters.
- coldstart-idle-15 iteration 10 (the only cold sample of that tier): the
  previous instance was shut down 166.3 s after iteration 9 (log 14:40:37 UTC,
  another instance of the service logged Shutting down at 14:42:28.9), and the
  instance that answered started its process ~14:42:29.6, ~620 s before our
  request (instance_uptime_s 622). Not caused by our request; reason unknown.
- 15 of 64 shutdown lines were logged 888.6-899.9 s after the previous
  response, yet all 19 valid requests of the 15 min tier (sent at exactly
  900 s) found a live instance.

Process mistakes to avoid next time:
- BigQuery billing export was never enabled in the Console (all exports
  "Disabled"); the dataset stayed empty, no per-service hourly cost exists, and
  the export is not backfilled. The Reports label filter listed no label keys.
  Cost of the day (whole project, no label filter, 2026-10-12): Cloud Run CPU
  528.1 s EUR 0.02, memory 528.1 GiB-s EUR 0.00, 140 requests EUR 0.00; the
  measurement VM (18.7 h) about EUR 0.17-0.18 plus small Network Intelligence
  Center items. Check that the export table exists BEFORE measuring.
- teardown3.csv was first generated in the wrong directory (empty file);
  gcloud logging --freshness must cover the run date (2d was too short after
  3 days, 7d used).
- Measurement VM deleted 2026-10-16 (instances, disks, addresses lists empty).
  No always-warm service exists in the project any more; the 7 remaining
  services all have min-instances unset (0).

