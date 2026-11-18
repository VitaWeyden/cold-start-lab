# Test 4 pilot - does `max-instances=1` cause the HTTP 429s?

Status: **run on 2026-11-06, 14:00:00 - 16:10:08 UTC; result: case C (429 not reproduced).**
All pilot files live in this folder (`Test 4 pilot/`); the v4 app is `Test 4 pilot/main.py`, built on top
of `app/` by `deploy4p.sh`.

Sections 1 - 8 were written before the run, so the decision rule in section 5 cannot be fitted
to the result afterwards; apart from this status line they are unchanged. The results are in
sections 9 - 14 (written 2026-11-18).

## 1. Why a pilot before Test 4

Test 4 (slide 22) will run 18 services for 10 hours with three application sizes, one of them
(size L) with a large model loaded at start-up. Test 3 left one problem open that would hit
Test 4 hardest: **36 of 120 attempts (30%) returned HTTP 429** ("The request was aborted because
there was no available instance"), all of them on requests that needed a new instance
(16 - 20 min idle), none at 15 min, none in Tests 1 and 2.

A re-read of the Test 3 logs (`Test 3/shutdown_logs.json`) adds one fact the Test 3 report did
not have: the log holds **108 shutdown lines**, one for every request that arrived after a
teardown, **including every 429**. After each 429 an instance with its own `instanceId` was
shut down 909 - 917 s after the 429 request started (after a normal cold request: 900 - 910 s
after the response). So the 429 request did start an instance, which became idle about 7 s
after the request arrived, while the client was still waiting (median 17 s) and then got the
429. The instance existed but the request was not handed to it. That is not what a lack of
capacity looks like (then no instance would start); it fits a routing / queueing limit, and
`max-instances=1` is the setting that limits how many instances the router may use.

Three things changed between Tests 1/2 (no 429) and Test 3, and Test 3 cannot separate them:

| | Tests 1-2 | Test 3 |
|---|---|---|
| time of day (UTC) | 18:12 - 04:33, 18:54 - ... (evening / night) | 12:22 - 19:05 (afternoon) |
| services cold-starting | 1 | 6, warm-ups in the same second |
| `max-instances` | 1 | 1 |

In Test 3 the 429s clustered in time: first attempts at 16 - 20 min had a 429 in 0/8 (12 UTC),
3/17 (13), 7/16 (14), 8/16 (15), 6/18 (16), 10/15 (17), 2/9 (18) cases.

Why this matters for Test 4: if the 429 is "a new instance is not ready in time and the
router may not use another one", then the size L app, which starts much more slowly, would get
429 on most cold attempts under `max=1`, and Test 4 would have no cold-start latency for exactly
the size it is about. A two-hour pilot answers this before 10 hours and 18 services are spent.

## 2. Question and hypothesis

**Q:** With everything else equal, does a scale-to-zero service with `max-instances=1` get more
429s on cold requests than the same service with `max-instances=3`?

**H1:** yes, the 429 rate is higher under `max=1`. **H0:** no difference.

The pilot also checks the new measurement plumbing on real Cloud Run before Test 4 depends on it
(section 6).

## 3. Design

| | |
|---|---|
| services | 6: `coldstart-p4-m1-{a,b,c}` (max 1) and `coldstart-p4-m3-{a,b,c}` (max 3) |
| identical | image `shop-api:v4`, app size S, 1 vCPU, **2 GiB**, concurrency 80, min-instances 0, no CPU boost, request-based billing, `europe-central2` |
| idle tier | 16 min (cold every time in Test 3: 64/64 valid samples at 16 - 20 min) |
| samples | 1 warm-up + 8 per service = **24 first attempts per arm** |
| start offsets | 0, 20, 40, 60, 80, 100 s, arms interleaved (m1-a, m3-a, m1-b, ...), so no two cold starts share a second |
| retries | after a 429 / 5xx / connection error: immediate retry, up to 5, each attempt its own CSV row |
| window | **14:00 - ~16:12 UTC** (15:00 - 17:12 in Bratislava, CET), a weekday |
| client | `e2-micro` VM in `europe-central2-a`, same as Tests 1 - 3 |
| endpoint | `/api/report` (same as Tests 1 - 3) |

Choices and why:

- **Three replicas per arm** instead of one service per arm: 8 samples per service fit into two
  hours; three replicas give 24 per arm, and a 429 tied to one particular service would show up
  as one replica being different.
- **16 min**: the shortest idle time that was always cold, so every sample needs a new instance
  (the 429s only happened there) and the run stays short.
- **2 GiB**: Test 4 runs all sizes at 2 GiB (size L needs it), and the pilot decides a Test 4
  setting, so it uses the Test 4 configuration. Consequence: if neither arm shows 429, the change
  from 1 GiB could be part of the reason (see section 7).
- **14 - 16 UTC**: the hours with the most 429s in Test 3. If the effect exists, this window has
  the best chance to show it; a night run could show "no 429" for the wrong reason.
- **Staggered starts**: in Test 3 all warm-ups went out in the same second; here no two cold
  starts overlap, so "many services cold-starting at once" is taken out of the explanation.
- **`max=3`, not unlimited**: enough room for the router to use a second instance, small enough
  that a scale-out bug cannot run up cost.

Sample size: with 24 vs 24 first attempts, 7 vs 0 429s (the Test 3 afternoon rate is ~30 - 60%)
gives a two-sided Fisher p of 0.009; 5 vs 0 is the smallest split that reaches p < 0.05
(p = 0.050), 4 vs 0 does not (p = 0.11). The pilot is sized to detect an effect of the Test 3
size, not a small one.

## 4. What is recorded

New in this run (the gaps from Test 3):

| need | how |
|---|---|
| exact instance matching | the app logs `instance_start`, `instance_ready`, `instance_shutdown`, one `request` line per request, and the Cloud Run instance id from the metadata server (`instance_identity`). Every log entry also carries `labels.instanceId`, so the app's `instance_id` joins with the Cloud Run instance. |
| why an instance started | Cloud Run system log (`varlog/system`, "Starting new instance. Reason: ...") |
| every request traceable | each attempt sends a 32-hex id as `X-Request-Id` (echoed by the app) and as trace id in `X-Cloud-Trace-Context` / `traceparent`, so client rows join with the request log, 429s included |
| what the user waits after a 429 | immediate retry, `time_to_success_ms` from the first attempt's start |
| cost per service | BigQuery billing export, checked by `measure4.py` before it sends anything; labels `experiment=pilot4`, `max_inst`, `replica` |
| time window | `--start-at`, actual start / end in `run4p_meta.txt` |

## 5. Decision rule (fixed before the run)

Counted on **first attempts** only, `max1` vs `max3`, two-sided Fisher exact test:

- **A - max-instances is the cause:** more 429s under `max1` and p < 0.05.
  Test 4 uses `max-instances=3` for all services; the paper says why, and that Tests 1 - 3 used 1.
- **B - not shown:** 429s in both arms, or no significant difference.
  Test 4 keeps `max-instances=1` (comparable with Tests 1 - 3), keeps the retry, and reports the
  429 as a failure mode of the platform, with the new logs as evidence for the next step.
- **C - not reproduced:** no 429 in either arm.
  Test 4 keeps `max-instances=1` and the retry; the 429 stays an open Test 3 observation, now
  also not seen at 2 GiB with staggered starts in the same afternoon hours.

`analyze4.py` prints which case applies.

## 6. Plumbing checks (must pass before Test 4)

1. `./setup_billing_export.sh check` says OK before the run, and the export has rows for the
   pilot hours a day later (`billing_query.sql` with `experiment = 'pilot4'`).
2. `analyze4.py` section 5: client attempts and request-log entries match on trace id. If
   nothing matches, Cloud Run does not keep our trace header and Test 4 needs another join key.
3. Section 6: every instance has an `app_id` (the `instance_identity` lookup works) and every
   teardown is matched to its own instance.
4. Section 8: "Starting new instance" lines exist. If the system log is empty, note it; the app
   log still gives start and shutdown.
5. `request id echoed by the app` = all successful attempts.

## 7. Limitations of the pilot

- One afternoon, one region, one app size; 24 samples per arm only detect an effect of the size
  seen in Test 3.
- The pilot runs at 2 GiB, Test 3 at 1 GiB. Case C cannot tell "the platform changed / that day
  was special" from "2 GiB or the staggered starts removed it".
- The image is `shop-api:v4` (`app/` with `Test 4 pilot/main.py`, i.e. logging added). The startup work is unchanged and the metadata
  lookup runs after `startup_ms` is taken, but the image is not byte-identical to Tests 1 - 3.

## 8. Run book

```bash
# --- day before (or at least a few hours before): billing export ---
cd "Test 4 pilot"                     # every command below runs from this folder
export PROJECT_ID=cold-start-lab REGION=europe-central2
./setup_billing_export.sh              # dataset + the Console step (Standard usage cost -> Enabled)
./setup_billing_export.sh check        # repeat until OK

# --- deploy (Cloud Shell) ---
./deploy4p.sh                          # builds shop-api:v4, 6 services, writes services4p.csv

# --- measurement VM (deleted after Test 3, so create it again) ---
gcloud compute instances create coldstart-measurer --zone europe-central2-a \
  --machine-type e2-micro --image-family debian-12 --image-project debian-cloud \
  --scopes cloud-platform              # cloud-platform: bq on the VM needs BigQuery access
gcloud compute scp measure4.py services4p.csv coldstart-measurer:~ --zone europe-central2-a
gcloud compute ssh coldstart-measurer --zone europe-central2-a

# --- on the VM ---
export PROJECT_ID=cold-start-lab
python3 measure4.py --services services4p.csv --dry-run      # must print "OK  billing export table found"
nohup python3 measure4.py --services services4p.csv --reps 8 \
  --start-at 2026-10-30T14:00Z --out results4p.csv --meta run4p_meta.txt > measure4p.log 2>&1 &
disown

# --- after the run (Cloud Shell, in Test 4 pilot/) ---
gcloud compute scp coldstart-measurer:~/results4p.csv coldstart-measurer:~/run4p_meta.txt \
  coldstart-measurer:~/measure4p.log . --zone europe-central2-a
START=2026-10-30T13:55:00Z END=2026-10-30T16:40:00Z ./collect_logs4.sh
python3 analyze4.py results4p.csv --logs logs4p
ONLY=pilot4 ../cleanup.sh                                  # delete the 6 pilot services
gcloud compute instances stop coldstart-measurer --zone europe-central2-a   # keep it for Test 4
```

The date in `--start-at` is an example: use the first weekday on which the billing check passes.
If `bq ls` fails on the VM with a permission error, the VM's service account needs
`roles/bigquery.dataViewer` and `roles/bigquery.jobUser` on the project.

Nothing else may call the pilot URLs during the run (browser tabs, uptime checks).

Expected cost: the six services are request-based, ~54 cold starts of a few seconds each,
well under EUR 0.01; idle instances after a 429 are not billed under request-based billing.
The VM is about EUR 0.01 per hour while it runs.

---

## 9. What was actually run

Run date: **2026-11-06 (Friday), 14:00:00.000 - 16:10:08 UTC** (15:00 - 17:10 in Bratislava,
CET), about 2 h 10 min. Started by `--start-at 2026-11-06T14:00Z` from the `coldstart-measurer`
VM in `europe-central2-a` (Python 3.11.2). Files in this folder: `results4p.csv`,
`run4p_meta.txt`, `measure4p.log`, `logs4p_{requests,app,system}.json`, `summary4p.csv`,
`instances4p.csv`, `analyze4p_output.txt` (output of `analyze4.py`), `billing4p.csv`
(output of `billing_query4.sql`).

As planned (section 3): 6 services, 1 warm-up + 8 samples each at 16 min idle, 54 requests,
first attempts per arm 24. Differences from the plan:

- **Date.** The plan's date (2026-10-30) was an example; the run used the first suitable weekday
  after the billing check had passed (`run4p_meta.txt`: export table found, last export
  2026-11-05 13:14 UTC).
- **Spacing between services.** The services started 20 s apart, but each next sample is sent
  16 min after the *end* of the previous response, so the offsets drift by the latency of each
  request. The smallest gap between two requests in the run was 8.8 s; the longest request took
  4.2 s, so **no two cold starts overlapped** (the purpose of the staggering still holds).
- **Idle time**: `actual_idle_s` = 960.0 s in all 48 samples.
- **No retries were needed**: 0 rows with `attempt > 1`, so `time_to_success_ms` equals the
  latency in every row.

## 10. Results

### 10.1 HTTP 429 per arm (first attempts)

| arm | n | OK | HTTP 429 | other failures | 429 rate | 95% Wilson CI | cold among OK |
|---|---|---|---|---|---|---|---|
| `max1` | 24 | 24 | 0 | 0 | 0% | 0 - 13.8% | 24/24 |
| `max3` | 24 | 24 | 0 | 0 | 0% | 0 - 13.8% | 24/24 |
| both | 48 | 48 | 0 | 0 | 0% | 0 - 7.4% | 48/48 |

Fisher exact test `max1` vs `max3`: **p = 1.0**. No 429 per replica (0/8 in all six services)
and per hour (14 UTC: 0/9 + 0/9, 15 UTC: 0/12 + 0/12, 16 UTC: 0/3 + 0/3). The six warm-up
requests (also cold) were all 200 as well, so the whole run is 0 of 54.

For comparison only (not a test defined in section 5, and the conditions differ, see 11):
Test 3 had 21 of 50 first attempts with 429 at 16 - 20 min in the same hours (14 - 16 UTC),
against 0 of 48 here (Fisher p < 1e-7). Whatever caused the Test 3 429s was not present in this
run.

### 10.2 Request, instance and log reconciliation

| | result |
|---|---|
| client attempts vs Cloud Run request log (trace id) | 54 vs 54: all matched, 0 client-only, 0 log-only, 0 status mismatches |
| request id echoed by the app | 54/54 |
| instances (`labels.instanceId` <-> app `instance_id`) | 54 instances, all with an `app_id`, each served exactly 1 request, **0 that never served a request** |
| "Starting new instance" in the system log | 54, 9 per service; reason `AUTOSCALING` in all of them |
| billing (section 11.2) | 54 billed requests, 9 per service |

So every request started its own new instance (`request_count` = 1, instance uptime 1.8 - 3.3 s
when the request was served), and every instance was used. In Test 3, an instance that never
served a request was left behind after every 429; here there is none, consistent with no 429.

The "Starting new instance" reason is always the generic `AUTOSCALING` text ("configured
scaling factors ... or no existing capacity for current traffic"), so the system log tells
*that* an instance started, not *why* beyond that.

### 10.3 Cold-start latency (successful first attempts, all cold)

| | `max1` | `max3` | both |
|---|---|---|---|
| n | 24 | 24 | 48 |
| total latency, median | 2646 ms | 3071 ms | 2929 ms |
| total latency, p95 / max | 3667 / 4204 ms | 3864 / 4053 ms | 3851 / 4204 ms |
| time to first byte, median | 2503 ms | 2930 ms | |
| DNS / TCP / TLS, median | 27 / 0.9 / 68 ms | 26 / 0.9 / 68 ms | |
| app `startup_ms`, median (range) | 1784 (1652 - 3069) ms | 2100 (1971 - 2686) ms | 2015 ms |
| app `init_ms`, median | 684 ms | 864 ms | |
| app `work_ms`, median | 94 ms | 137 ms | |

The warm-ups (first start of each new revision) took 3.4 - 6.6 s, with `startup_ms`
2.3 - 4.5 s; they are not analysed, as in Tests 1 - 3.

### 10.4 Teardown

Teardown = app `instance_shutdown` minus the end of the instance's last response, matched on the
exact instance (54 of 54):

| | seconds |
|---|---|
| minimum (without the outlier below) | 900.4 |
| median | 905.2 |
| maximum | 910.0 |
| below 900 s | 0 of 53 |

The 15-minute idle timeout from Test 3 (889 - 910 s) is confirmed, the same in both arms
(median 905.6 s `max1`, 905.3 s `max3`), now on 2 GiB and with the exact instance join instead
of the Test 3 time-window match. Unlike Test 3 (15 of 64 shutdown lines before 900 s), no
shutdown was logged before 900 s.

**One outlier: 297.5 s.** The warm-up instance of `coldstart-p4-m1-a` (started 14:00:01, served
the warm-up at 14:00:05) logged `Shutting down` at 14:05:03, about 5 minutes after its only
request. The system log has no entry for it besides its start and startup probe, and the
request log has no request on that service between 14:00:05 and the next sample at 14:16:06,
so unlike the early teardown in Test 3 (section 3.4 there: traffic from outside the
measurement), outside traffic is ruled out here. The cause is unknown. It did not affect the
data: the next sample came 16 min later and would have been cold anyway.

## 11. Additional findings (not part of the decision rule)

### 11.1 `max3` cold starts were slower

The `max3` arm was about 0.4 s slower per cold start (median 3071 vs 2646 ms), and the
difference is inside the container, not in the network: `startup_ms` +316 ms, `init_ms`
+180 ms, `work_ms` +43 ms, DNS / TCP / TLS identical. A `max3` sample was slower than a `max1`
sample in 87% of all pairs.

How much this means is limited:

- Per sample it is clear (permutation test on the medians, p < 0.001), but the samples are
  grouped in three services per arm, and per service the arms overlap: median latency
  `m1-c` 2525, `m1-b` 2608, `m3-c` 2945, `m1-a` 2949, `m3-a` 3047, `m3-b` 3546 ms. At the
  service level (3 vs 3, rank sum) the difference is not significant (p = 0.2).
- `max-instances` is a routing / scaling limit; there is no known mechanism by which it would
  slow down Python imports and the CPU work inside an already started instance. Instances
  placed on different hosts are a more likely explanation, but the pilot cannot show it.
- The arms were interleaved in time, so time of day does not explain it.

It is reported as an exploratory observation. It does not change the decision (Test 4 uses
`max-instances=1` anyway), but it means that cold-start differences of a few hundred ms between
services in Test 4 should be compared against this service-to-service spread.

### 11.2 Cost (BigQuery billing export, `billing4p.csv`)

Rows with `experiment=pilot4` on 2026-11-06, 14:00 - 16:00 UTC hours, gross:

| | `max1` | `max3` | total |
|---|---|---|---|
| billed requests | 27 | 27 | 54 |
| request-based vCPU-s | 97.5 | 108.9 | 206.4 |
| gross cost | EUR 0.0036 | EUR 0.0040 | **EUR 0.0075** |
| net cost (after free tier credits) | 0 | 0 | **0** |

- The labels arrive in the export, so cost per service and per arm can be read in Test 4.
- Memory is billed at exactly 2.0 GiB per vCPU-s, matching the 2 GiB setting.
- The `max3` arm paid ~12% more vCPU time, the same direction as its slower start-up (11.1):
  in the 15:00 hour (12 requests per arm, no warm-ups) 2.48 vs 2.98 vCPU-s per request. Under
  request-based billing the instance start-up is billed, so a slower cold start costs more.
- The estimate in section 8 ("well under EUR 0.01") held.
- The export also has one request per pilot service on 2026-11-05 in the 16:00 UTC hour, i.e.
  on deploy day, before the run; it is outside the run window and not counted above.
- The measurement VM is not in this query (no labels); it was stopped after the run.

## 12. Plumbing checks (section 6)

| # | check | result |
|---|---|---|
| 1 | billing export OK before the run, rows for the pilot hours afterwards | **pass**: gate passed (`run4p_meta.txt`), rows with all labels present (11.2) |
| 2 | client attempts join the request log on trace id | **pass**: 54/54 |
| 3 | every instance has an `app_id`, every teardown matched to its own instance | **pass**: 54/54, 54/54 |
| 4 | "Starting new instance" lines exist | **pass**: 54, but the reason is always the generic `AUTOSCALING` text |
| 5 | request id echoed = all successful attempts | **pass**: 54/54 |

The measurement plumbing works on real Cloud Run and can be used for Test 4 unchanged.

## 13. Decision

**Case C - not reproduced** (section 5): no 429 in either arm.

- Test 4 keeps **`max-instances=1`**, comparable with Tests 1 - 3.
- Test 4 keeps the **immediate retry** (up to 5) and records every attempt, so if 429s come back
  with the slow-starting size L, the user-visible wait (`time_to_success_ms`) is still measured.
- The Test 3 429s stay an **open observation**. As stated in section 7, case C cannot separate
  the possible reasons: compared with Test 3, this run had 2 GiB instead of 1 GiB, staggered
  instead of simultaneous cold starts, the v4 image, and a different day. Test 4 uses 2 GiB and
  the v4 image as well; if it also staggers the cold starts of its services, it runs under the
  same conditions that produced no 429 here.

What the pilot does show: under `max-instances=1`, 24 of 24 cold starts at 16 min idle in the
14 - 16 UTC hours were served without error, so `max-instances=1` alone does not produce the
429s.

## 14. Notes for the next run

- `gcloud compute scp` on Windows uses PuTTY and accepts only one remote file per call
  ("Multiple remote sources not supported by PuTTY"); run the download from Cloud Shell, or
  one file per call.
- `bq query "$(cat billing_query4.sql)"` fails, because the file starts with a `--` comment
  that `bq` reads as a flag; strip the comment lines first. The query also only looks back
  7 days (`INTERVAL 7 DAY`); run it within a week of the measurement or widen the interval.
  Used here:
  ```bash
  SQL="$(sed -e '/^[[:space:]]*--/d' -e 's/PROJECT/cold-start-lab/g' -e 's/INTERVAL 7 DAY/INTERVAL 30 DAY/' billing_query4.sql)"
  bq query --nouse_legacy_sql --format=csv --max_rows=1000 "$SQL" > billing4p.csv
  ```
- Log window used for `collect_logs4.sh`: `START=2026-11-06T13:55:00Z END=2026-11-06T16:40:00Z`
  (the last shutdown is ~15 min after the last request, 16:25 here).
- After the run: pilot services deleted (`ONLY=pilot4 ../cleanup.sh`), VM stopped and kept for
  Test 4.
