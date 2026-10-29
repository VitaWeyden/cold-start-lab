# Test 4 pilot - does `max-instances=1` cause the HTTP 429s?

Status: planned, not run yet. All Test 4 files live in this folder (`Test 4/`); the v4 app is
`Test 4/main.py`, built on top of `app/` by `deploy4p.sh`.
 Written before the run, so the decision rule in section 5 cannot
be fitted to the result afterwards.

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
- The image is `shop-api:v4` (`app/` with `Test 4/main.py`, i.e. logging added). The startup work is unchanged and the metadata
  lookup runs after `startup_ms` is taken, but the image is not byte-identical to Tests 1 - 3.

## 8. Run book

```bash
# --- day before (or at least a few hours before): billing export ---
cd "Test 4"                           # every command below runs from this folder
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

# --- after the run (Cloud Shell, in Test 4/) ---
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