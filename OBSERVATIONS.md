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
