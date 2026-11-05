# Test 3 report - where exactly does scale-to-zero tear the instance down?

Run date: 2026-10-12, 12:22:48 - 19:05:39 UTC (14:22 - 21:05 CEST), about 6 h 43 min.
Region: `europe-central2`. Files: `Test 3/results3.csv`, `teardown3.csv`, `summary3.csv`,
`shutdown_logs.json`, `services3.csv`; cost: `billing/billing_hourly_2026-10.csv`,
`billing/billing_windows.py` (section 4 updated on 2026-10-29).

## 1. Question

Test 2 narrowed the point where a scale-to-zero Cloud Run service loses its instance to
(15, 20] minutes of idle time. Test 3 resolves it in 1-minute steps (15, 16, 17, 18, 19, 20 min)
and additionally reads the real teardown moment from Cloud Logging.

## 2. Setup (what was actually run)

- Six services `coldstart-idle-15` ... `coldstart-idle-20`, deployed by `deploy3.sh` from the
  config of `coldstart-scale-zero` (same image, 1 vCPU, 1 GiB, concurrency 80), with
  `min-instances` unset (= 0), `max-instances=1`, no CPU boost.
- All six measured in parallel from one VM in the same region (`measure3.py`, 20 samples per
  tier). Every service got a warm-up request first (not analysed), so every sample is
  "N minutes after a known previous request". The idle time is the measured monotonic-clock gap
  between the end of the previous response and the start of the next request
  (`actual_idle_s`; it equals the nominal tier in every row).
- Per request: DNS / TCP / TLS / time-to-first-byte / body (IPv4 only) plus the server-reported
  `cold`, `instance_id`, `instance_uptime_s`, `startup_ms`, `init_ms`, `work_ms`.
- Teardown moment: for each cold sample, the uvicorn `Shutting down` log line (written on
  SIGTERM) between the previous response and the new request (`teardown_times.py`).
  All 65 cold samples were matched to a log line.

## 3. Results

### 3.1 Cold rate per idle tier (`summary3.csv`)

| idle (min) | valid samples | HTTP 429 | cold | cold rate | 95% Wilson CI |
|---|---|---|---|---|---|
| 15 | 20 | 0 | 1 | 5.0% | 0.9 - 23.6% |
| 16 | 14 | 6 | 14 | 100% | 78.5 - 100% |
| 17 | 17 | 3 | 17 | 100% | 81.6 - 100% |
| 18 | 10 | 10 | 10 | 100% | 72.2 - 100% |
| 19 | 10 | 10 | 10 | 100% | 72.2 - 100% |
| 20 | 13 | 7 | 13 | 100% | 77.2 - 100% |

The cold rate steps from 1/20 at 15 min to 14/14 at 16 min. Only one tier has mixed outcomes,
so a logistic fit has no finite solution; the result is the crossing bracket
**between 15 and 16 minutes of idle time**, i.e. a teardown at roughly 15 minutes.

The single cold sample at 15 min (iteration 10) is not an idle-timer teardown, see 3.4.

### 3.2 When Cloud Run sends SIGTERM (`teardown3.csv`)

64 of the 65 cold samples fall in one narrow band, measured from the end of the previous
response:

| | seconds | minutes |
|---|---|---|
| minimum | 888.6 | 14.81 |
| 5th percentile | 891.4 | 14.86 |
| median | 903.3 | 15.05 |
| 95th percentile | 908.9 | 15.15 |
| maximum | 910.1 | 15.17 |

The per-tier medians are 903.6 (16), 904.3 (17), 901.0 (18), 905.6 (19), 900.7 (20) s, i.e. the
tier does not matter, as expected for a fixed idle timeout. The 65th value is the
166.3 s outlier from 3.4.

### 3.3 Latency, valid samples only

| | n | median | p95 | max |
|---|---|---|---|---|
| cold request (total) | 65 | 3375 ms | 5888 ms | 6978 ms |
| warm request (total) | 19 | 251 ms | - | 291 ms |

For cold requests the median time-to-first-byte is 3237 ms against 114 ms for warm ones; DNS,
TCP and TLS are the same in both (about 23 / 0.7 / 67 ms). The app's own `startup_ms` for the
new instance has a median of 2320 ms (range 1968 - 4820 ms), `init_ms` about 770 ms.
A cold request is about 13x slower than a warm one.

### 3.4 Observations that are not explained by the idle timeout

**HTTP 429 on cold starts.** 36 of the 120 attempts (30%) returned `429`, all of them at the
tiers 16 - 20 where the instance had been torn down (16: 6, 17: 3, 18: 10, 19: 10, 20: 7; none
at 15). Cloud Logging records them as "The request was aborted because there was no available
instance" with a request latency of 0 s, while the client waited 10.2 - 22.8 s (median 17.4 s)
before the error came back. Tests 1 and 2 did not show this. The services were deployed with
`max-instances=1`; whether that setting or something else causes the 429s was **not tested**,
so no cause is claimed. They are reported as a separate failure mode, are not counted as cold
or warm, and are the reason the valid sample counts are below 20.

**The 15-minute tier's one cold sample (iteration 10).** The instance that served iterations
1 - 9 was shut down at 14:40:37 UTC, only 166 s after iteration 9 (a second instance of that
service logged `Shutting down` at 14:42:28.9). The instance that answered iteration 10 started
its process at about 14:42:29.6 UTC, roughly 620 s before our request, so our request did not
start it. The idle timer was therefore not what removed the instance; the data does not show
what did (a platform-side replacement is a possibility, not something we can verify).
Excluding this sample, the 15-minute tier is 19/19 warm.

*Added 2026-10-29:* the hourly billing export shows **2 billed requests on `coldstart-idle-15`
in the 14:00 UTC hour that our client did not send** (6 billed, 4 sent) together with 12.8
vCPU-s of request-based time, against 0.3 - 1.3 vCPU-s in the other warm hours of that service.
That is about two extra instance starts, in the same hour as the two shutdowns at 14:40:37 and
14:42:28.9. So the most likely trigger of the early replacement is traffic from outside the
measurement (the services were public, `--allow-unauthenticated`), not the platform. Who sent
it can be read from the Cloud Run request log of that hour while the logs are still kept
(30 days, i.e. until about 2026-11-11).

**Teardown before 900 s.** 15 of the 64 shutdown log lines were written before 900 s after the
previous response (earliest 888.6 s, in `coldstart-idle-19`), yet all 19 requests of the 15-min
tier, sent at exactly 900 s, were answered by a live instance. So a `Shutting down` line a few
seconds before 900 s does not by itself mean that an incoming request at 900 s finds no
instance. The log timestamp and the moment the instance stops serving are not necessarily the
same, and we did not measure the difference.

## 4. Cost

*Rewritten 2026-10-29.* At the time of the run the BigQuery billing export was still
"Disabled". It was switched on later and, contrary to what we expected, it contains hourly data
back to 2026-09-01, with the service labels. So the per-service cost of Test 3 exists after
all (`billing/billing_windows.py`, hours 12 - 19 UTC on 2026-10-12, gross EUR):

| service | billed requests | client attempts | of them 429 | request-based vCPU-s | cost |
|---|---|---|---|---|---|
| `coldstart-idle-15` | 24 | 21 | 0 | 21.1 | €0.00069 |
| `coldstart-idle-16` | 22 | 21 | 6 | 97.5 | €0.00318 |
| `coldstart-idle-17` | 22 | 21 | 3 | 85.2 | €0.00278 |
| `coldstart-idle-18` | 22 | 21 | 10 | 103.1 | €0.00336 |
| `coldstart-idle-19` | 24 | 21 | 10 | 113.3 | €0.00369 |
| `coldstart-idle-20` | 22 | 21 | 7 | 98.6 | €0.00321 |
| **Test 3 total** | **136** | **126** | **36** | **518.8** | **€0.0169** |
| no label (12:00 hour) | 4 | - | - | 9.3 + 227.4 s min instance | €0.0017 |

Prices: 2.9499e-05 EUR per vCPU-s and 3.0187e-06 EUR per GiB-s (request-based, Tier 2).

What the bill adds to the findings:

- **The 429 requests cost money too.** Every service paid far more vCPU time than its successful
  responses took (e.g. idle-16: 97.5 vCPU-s billed vs 53 s of successful responses), because
  request-based billing includes the start-up time of every instance, including the ones the
  429 requests started (see 3.4 and `OBSERVATIONS.md` 2026-10-29: one instance per request
  after a teardown, 429s included).
- **140 vs 126 requests.** 4 of the 140 belong to an unlabelled service in the 12:00 hour,
  together with 227.4 s of min-instance time (a min-instances >= 1 service that existed for a
  few minutes; the always-warm service itself had stopped billing on 2026-10-04). Of the
  remaining 10 extra requests on the labelled services, 8 fall in the 12:00 hour (before or
  around the 12:22 start, i.e. checks of the new services) and 2 in the 14:00 hour on
  `coldstart-idle-15` (see 3.4).
- The min-instance lines of the day (227.4 s) are therefore not Test 3, as stated before.

The day-level Reports view that this section used before, for reference (whole project):

| SKU | usage | cost |
|---|---|---|
| Cloud Run services CPU (request-based) | 528.1 s | EUR 0.02 |
| Cloud Run services memory (request-based) | 528.1 GiB-s | EUR 0.00 |
| Cloud Run requests | 140 | EUR 0.00 |
| Cloud Run min-instance CPU / memory | 227.41 s / GiB-s | EUR 0.00 |

The measurement VM is separate infrastructure, not experiment cost: €0.094 during the run
(12 - 19 UTC, Compute Engine + Network Intelligence Center), but it kept running until it was
deleted, and those idle days cost €0.90, ten times the run itself.

## 5. Conclusions for the paper

1. A scale-to-zero Cloud Run service keeps its instance for about **15 minutes (about 900 s;
   observed 889 - 910 s)** after the last request. 16 minutes of idle time were cold in 14 of 14
   valid samples, 17 - 20 minutes in 50 of 50.
2. The cold start costs about **3.4 s** (median) against about **0.25 s** warm, a factor of ~13;
   the 1.97 - 4.82 s of container startup account for most of it.
3. At the boundary itself the outcome depends on a race of a few seconds, so any workload that
   pauses for roughly 15 minutes will sometimes see cold starts and sometimes not.
4. A service that is not kept warm must expect more than latency: 36 of the 120 attempts after a
   teardown (30%) failed with 429 under `max-instances=1`. This was not seen in Tests 1 and 2 and its
   cause is open.

## 6. Limitations

- One region, one night, one configuration (`max-instances=1`, 1 vCPU, 1 GiB, no CPU boost).
- The valid sample sizes at 18 and 19 min are 10 (CI lower bound 72%); the 429s reduced them.
- Teardown time is measured from the client-side end of the previous response, which differs
  from the server's view by network time and body transfer (well under a second here).
- Cost per service is exact per hour, not per request: the export has no finer resolution.
- The causes of the 429s, of the 166 s teardown at tier 15 and of the early shutdown lines were
  not investigated further at the time (see the 2026-10-29 additions in 3.4 and 4, and the
  Test 4 pilot).

## 7. Reproduce

```bash
python3 analyze3.py results3.csv
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name=~"^coldstart-idle-" AND textPayload:"Shutting down"' \
  --project cold-start-lab --freshness=7d --format=json > shutdown_logs.json
python3 teardown_times.py results3.csv shutdown_logs.json > teardown3.csv
```

(`--freshness` must cover the run date; the logs are kept for 30 days by default.)