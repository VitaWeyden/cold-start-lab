# Test 4 - how does the size of the application change cold starts, teardown and cost?

Status: **main run done on 2026-11-19 (08:00 - 17:58 UTC, scale-to-zero, 18 services); always-warm addition (section 10) run 2026-11-20 19:00 - 2026-11-21 04:53 UTC.** Results: sections 11 - 18 (written 2026-11-21; the always-warm cost W4 is pending the billing export). Written before the run, so the analysis rules in section 5
cannot be fitted to the result afterwards. All Test 4 files live in this folder (`Test 4/`).
The preparation run is in `Test 4 pilot/` (its report: `Test 4 pilot/Test4_report.md`).

## 1. Question

Tests 1 - 3 measured one application (the webshop API, here called **size S**). Test 3 found
that a scale-to-zero Cloud Run service keeps its instance for about 15 minutes (889 - 910 s)
and that a cold start of size S costs about 3.4 s. Test 4 asks how these numbers change when
the application is bigger:

1. **Cold start:** how much longer does a bigger application take to answer its first request
   after a teardown, and which part of the start-up grows (the application's own start-up work,
   or the platform's part before the application runs, e.g. the larger image)?
2. **Teardown / restart:** does the moment the platform removes an idle instance shift with the
   size of the application, or is the ~15-minute idle timeout fixed?
3. **Cost:** how much does one cold start cost per size under request-based billing, which bills
   the start-up time too?

Expectations (written down so they can be wrong): (1) cold-start latency grows with size,
mostly through the application's start-up work; (2) the teardown moment does not depend on size
(about 900 - 910 s, as in Test 3 and the pilot); (3) the billed time per cold start grows with
size in the same proportion as the start-up time; (4) warm requests cost the same for every size,
because the work per request is the same.

## 2. The three sizes

One code base (`main.py`) and one `Dockerfile` with a build argument `SIZE`, three images
`shop-api:v4-s / v4-m / v4-l`. All sizes run the same `/api/report` work on the same 200,000
orders, so **only the start-up differs**; the measured request is identical.

| | S | M | L |
|---|---|---|---|
| what it is | the app of Tests 1 - 3 and the pilot | S + a heavier dependency + more data + a small model | M + a large model |
| dependencies | FastAPI, pandas, NumPy | + scikit-learn (with SciPy, joblib) | same as M |
| data built at start-up | 200,000 orders -> SQLite | 1,000,000 orders -> SQLite | 1,000,000 orders -> SQLite |
| model loaded at start-up | - | RandomForest, 20.8 MB | RandomForest, 503.6 MB |

The models are generated at image build time (`make_model.py`, fixed seed, no download): a
RandomForestRegressor on synthetic order data, trees added until the file reaches the target
size. This was chosen over an external database or cache (the earlier idea for size M), because
it needs no VPC, adds no always-on billed service and adds no second service's latency to the
measurement.

**Local calibration** (`local_check4.sh` -> `local_check4.csv`, Docker Desktop, limits 1 CPU /
2 GiB like Cloud Run, 3 starts per size, 2026-11-18). Local numbers are not Cloud Run numbers
(size S starts in ~1.2 s locally and ~2.0 s on Cloud Run in the pilot); they only show that the
sizes are clearly apart and that L fits into 2 GiB:

| | S | M | L |
|---|---|---|---|
| image size (local, uncompressed) | 392 MB | 693 MB | 1,298 MB |
| `startup_ms` (median of 3) | 1,214 ms | 3,807 ms | 5,679 ms |
| - imports (FastAPI, pandas) | ~760 ms | ~710 ms | ~770 ms |
| - data + SQLite (`init_ms`) | ~440 ms | ~2,380 ms | ~2,370 ms |
| - scikit-learn import | - | ~660 ms | ~680 ms |
| - model load | - | ~20 ms | ~1,780 ms |
| memory after start-up (RSS) | 155 MB | 406 MB | 1,371 MB |
| peak memory during start-up | 186 MB | 555 MB | 1,371 MB |
| `/api/report` work | ~61 ms | ~56 ms | ~84 ms |

L uses 67% of the 2 GiB limit (a first try with a 600 MB model peaked at 1,580 MB = 77%; it was
reduced to keep a margin against an out-of-memory restart, which would show up as 5xx and mix a
second failure mode into the data). The `/api/report` work of L is ~25 ms slower locally despite
identical work, probably memory/cache effects; it is recorded (`work_ms`) and subtracted where
start-up is compared.

## 3. Design

| | |
|---|---|
| services | **18 = 3 sizes x 6 idle tiers**, `coldstart-t4-{s,m,l}-{05,14,15,16,18,20}` |
| idle tiers | **5 min** (warm baseline), **14, 15, 16 min** (around the ~900 s teardown), **18, 20 min** (cold every time in Test 3) |
| identical | 1 vCPU, 2 GiB, concurrency 80, min-instances 0, **max-instances 1** (pilot result, case C), no CPU boost, request-based billing, `europe-central2`, endpoint `/api/report` |
| per service | 1 warm-up, then samples exactly *tier* minutes after the end of the previous response, until the 10-hour window ends |
| start offsets | 30 s apart, in the order tier by tier and S, M, L within a tier (0 ... 510 s) |
| retries | after a 429 / 5xx / connection error: immediate retry, up to 5 (as in the pilot) |
| window | **10 h, 08:00 - 18:00 UTC** (09:00 - 19:00 Bratislava, CET) on a weekday |
| client | `coldstart-measurer` (e2-micro, `europe-central2-a`), same VM as Tests 1 - 3 and the pilot |
| labels | `experiment=test4`, `size`, `idle_min` (billing export) |

Expected samples per service in 10 h: 5 min ~119, 14 min ~42, 15 min ~39, 16 min ~37,
18 min ~33, 20 min ~29. Per size that is ~100 certainly-cold samples (16, 18, 20 min) from three
different services, ~120 warm samples, and the 14 / 15 / 16 min tiers for the boundary.

Choices and why:

- **Sizes x tiers instead of replicas.** Every size runs on six services, and its cold samples come
  from three services (16, 18, 20 min). The pilot showed a ~400 ms difference between services that
  were configured identically (`Test 4 pilot/Test4_report.md` 11.1), so a size effect must hold
  across services, not only across samples of one service (rule in 5.1).
- **14 / 15 / 16 min.** Test 3 put the teardown at 889 - 910 s after the last response. If a bigger
  application were removed earlier, 14 or 15 min would turn cold for that size; if later, 16 min
  would stay warm. These three tiers show a shift in either direction.
- **5 min** gives the warm latency per size. With identical work it should be the same for all
  sizes; if not, the work itself differs and the cold-start comparison must account for it.
- **max-instances 1, 2 GiB, staggered starts, retry**: the pilot settings, under which no 429
  occurred (0 / 54). With the same settings the results stay comparable with Tests 1 - 3 and the
  pilot.
- **08:00 - 18:00 UTC.** The pilot showed no 429 in 14 - 16 UTC, so the afternoon does not have to
  be avoided; the window includes the Test 3 peak hours (14 - 17 UTC) and is in working hours, so
  the run can be watched.
- **Overlapping cold starts are not prevented.** The idle time must be exact (at 14 / 15 / 16 min a
  few seconds matter), so the samples cannot be shifted into free slots. With ~15 services in cold
  tiers, two cold starts will sometimes overlap. This is recorded (5.6), not avoided.

## 4. What is recorded

Everything the pilot recorded (its section 4: request id = trace id, instance join on
`labels.instanceId`, app instance logs, system log, retries, billing gate, `run4_meta.txt`),
plus, from the app, in every response and in the `instance_start` log line:

| field | meaning |
|---|---|
| `app_size` | the size the app reports (checked against the services file) |
| `startup_ms` | process start -> ready to serve, all of it |
| `imports_ms`, `init_ms`, `ml_import_ms`, `model_load_ms` | its phases (FastAPI + pandas imports; data + SQLite; scikit-learn import; model load) |
| `model_mb` | model file size |
| `peak_rss_mb`, `rss_mb` | peak and current memory of the instance |

Image sizes in Artifact Registry are read once after the build (`gcloud artifacts docker images
list ... --format='value(package,tags,metadata.imageSizeBytes)'`).

## 5. Analysis rules (fixed before the run)

Samples are the first attempts of measured requests (warm-ups excluded). "Cold" means the app says
`cold=true` (first request of a new instance), as in Tests 1 - 3.

### 5.1 Cold-start latency per size

Cold samples of all tiers, per size: n, median, p95 of the total latency and of time to first
byte. Pairwise S-M, M-L, S-L with a Mann-Whitney test (Holm-corrected). A difference between two
sizes is called **shown** only if both hold:

- the sample test gives p < 0.05 after correction, and
- at the service level, all three cold-tier services (16, 18, 20 min) of the bigger size have a
  higher median than all three of the smaller one (3 vs 3 complete separation: one-sided p = 0.05).

The second condition is the pilot lesson: samples of one service are not independent evidence
about the size.

### 5.2 Which part grows

For each cold sample: `startup_ms` and its phases (from the app), and
`platform_ms = ttfb_ms - startup_ms - work_ms`, i.e. the part of the wait that is not the
application's own work (instance scheduling, image fetch, container start, network). Medians per
size. If `platform_ms` grows from S to L by more than 500 ms, the image size (or something else
outside the app) is reported as contributing; otherwise the growth is attributed to the app's
start-up work.

### 5.3 Teardown and restart

- Teardown time per instance = its `instance_shutdown` minus the end of its last response
  (exact instance join, as in the pilot). Per size: n, min, median, max.
- A **shift** is called only if a size's median differs from S by **at least 10 s** and the
  Mann-Whitney test gives p < 0.05 (Holm, S-M and S-L). (Test 3 and the pilot: 889 - 910 s, so
  10 s is beyond the normal spread.)
- Cold rate at 14, 15 and 16 min per size with 95% Wilson intervals; S vs L with a Fisher test at
  each of these tiers.
- Early teardowns (< 800 s, like the pilot's 297 s) are listed with their logs and not mixed into
  the medians.

### 5.4 Warm latency (5 min)

Median and p95 per size. Expected equal; a difference above 100 ms in median is reported and the
work per request (`work_ms`) is checked.

### 5.5 Cost

From the billing export (`billing_query4.sql`), per service and hour: billed requests,
request-based vCPU-s and GiB-s, gross and net EUR.

- Billed requests per service must equal the client attempts (pilot: 54 = 54).
- **vCPU-s per cold start** per size = billed vCPU-s of the 16 / 18 / 20 min services divided by
  their number of requests (all of them cold), compared with the median `startup_ms` + latency.
- EUR per 1,000 cold starts per size (gross, Tier 2 prices).

### 5.6 Failures and overlaps

- HTTP 429 / 5xx / errors per size and per hour, with `time_to_success_ms` for the failed ones.
- Out-of-memory: any `instance_start` without a matching `instance_shutdown`, or a system log
  entry about memory, is listed per size.
- Overlap: a cold sample is marked "overlapping" if another service's cold request was in flight
  during it. The cold-latency medians of 5.1 are given with and without the overlapping samples;
  if they differ by more than 10%, this is reported.

## 6. Plumbing checks (before and after the run)

1. `./local_check4.sh` passed (2026-11-18; section 2).
2. `deploy4.sh`: config check shows min 0, max 1, 1 CPU, 2 GiB, no boost, and the right image
   tag on all 18 services.
3. Before the run: `measure4.py --dry-run` says `OK billing export table found`.
4. After the run: client attempts = request-log entries (trace id) = billed requests per service.
5. `app_size` equals the services file's `size` on every successful attempt.
6. Every instance has an `app_id` and every teardown is matched to its own instance.

## 7. Limitations (known before the run)

- "Size" is a bundle: from S to M the dependencies, the data and a small model change together;
  from M to L the model (and with it the image) grows. The phase timing (5.2) separates the parts
  of the start-up, but not their causes beyond that.
- The models are synthetic RandomForests; a real model (e.g. a neural network loaded with its
  framework) may load differently. The point here is a controlled, repeatable start-up cost.
- One region, one day, one configuration (1 vCPU, 2 GiB, max-instances 1, no CPU boost).
- The local calibration (section 2) is on a different machine and is not compared with Cloud Run.
- The work of `/api/report` is identical by design, but L measured ~25 ms slower locally (2).

## 8. Cost estimate (before the run)

Request-based billing: about 100 - 140 cold starts per size, assuming ~2.5 s (S), ~6 s (M) and
~9 s (L) billed per cold start on Cloud Run (local start-up x ~1.6, as for S between local and the
pilot): ~2,400 vCPU-s, i.e. ~EUR 0.07 CPU + ~EUR 0.015 memory; the ~360 warm requests add little.
This is within the monthly free tier, so the net cost is expected to be ~0. Around it: the VM
(~10.5 h, ~EUR 0.10), Cloud Build (default machine, within the free build minutes), and Artifact
Registry storage for the three images (~EUR 0.1 per month beyond the free 0.5 GB). The measured
cost is a result of the test (5.5).

## 9. Run book

```bash
# --- Cloud Shell: build + deploy (from the repository root or anywhere) ---
cd ~/cold-start-lab && git pull && cd "Test 4"
export PROJECT_ID=cold-start-lab REGION=europe-central2
./deploy4.sh                       # builds v4-s/m/l (~10-15 min, L model at build), 18 services, services4.csv
gcloud artifacts docker images list ${REGION}-docker.pkg.dev/${PROJECT_ID}/coldstart-lab/shop-api \
  --include-tags --format='table(tags,metadata.imageSizeBytes)'      # image sizes for 5.2

# --- measurement VM (stopped after the pilot) ---
gcloud compute instances start coldstart-measurer --zone europe-central2-a
gcloud compute scp measure4.py services4.csv coldstart-measurer:~ --zone europe-central2-a
gcloud compute ssh coldstart-measurer --zone europe-central2-a

# --- on the VM ---
export PROJECT_ID=cold-start-lab
python3 measure4.py --services services4.csv --dry-run         # "OK billing export table found"
nohup python3 measure4.py --services services4.csv --duration-h 10 \
  --start-at <DATE>T08:00Z --out results4.csv --meta run4_meta.txt > measure4.log 2>&1 &
disown

# --- after the run (Cloud Shell, in Test 4/) ---
gcloud compute scp coldstart-measurer:~/results4.csv coldstart-measurer:~/run4_meta.txt \
  coldstart-measurer:~/measure4.log . --zone europe-central2-a
START=<DATE>T07:55:00Z END=<DATE>T18:30:00Z ./collect_logs4.sh
python3 analyze4.py results4.csv --logs logs4 | tee analyze4_output.txt
gcloud compute instances stop coldstart-measurer --zone europe-central2-a
# the next day: billing (set the run date in the file first)
bq query --nouse_legacy_sql --format=csv --max_rows=10000 "$(cat billing_query4.sql)" > billing4.csv
python3 analyze4.py results4.csv --logs logs4 --billing billing4.csv | tee analyze4_output.txt
# when everything is downloaded: delete the 18 services (and, if it was the last test, everything)
../tools/cleanup.sh                  # all coldstart-* services; DELETE_VM=1 KEEP_IMAGES=0 for everything
```

`<DATE>`: a weekday at least a day after the deploy. Download the results from Cloud Shell
(`cloudshell download`, or zip first); if that fails, run the `bq` step locally (pilot notes, 14).

Nothing else may call the Test 4 URLs during the run (browser tabs, uptime checks): every extra
request resets the idle timer of that service. The deploy itself starts one instance per service
(the pilot's billing shows 1 request per service on deploy day, outside the run); deploying at
least an hour before the start keeps it out of the measurement.

## 10. Addition: the always-warm side (planned 2026-11-20, after the main run)

**Why this was added.** The project compares scale-to-zero with an always-warm instance
(`min-instances=1`), and Tests 1 - 2 measured both. The Test 4 design in sections 3 - 9 has only
scale-to-zero services: the always-warm side was left out by mistake when the design was written,
and this was noticed only after the main run on 2026-11-19. It is added as a separate run with the
same images and settings. The main run's results were already known when this section was
written; the rules below are fixed before the always-warm run, but they were not part of the
original plan, and the paper should say so.

### 10.1 Design

| | |
|---|---|
| services | **3**: `coldstart-t4w-{s,m,l}-16`, one per size |
| identical to Test 4 | images `shop-api:v4-s/m/l` (the same build), 1 vCPU, 2 GiB, concurrency 80, max-instances 1, no CPU boost, request-based billing, `europe-central2`, `/api/report`, same client VM, retry |
| different | **min-instances 1** (always warm), labels `experiment=test4w` |
| idle tier | **16 min**, the first tier that was cold every time for every size in the main run (37 / 36 / 36 of 37 / 36 / 36) |
| window | **10 h, 2026-11-20 19:00 - 2026-11-21 05:00 UTC** (Friday evening to Saturday morning) |
| offsets | 0, 30, 60 s |
| deploy / delete | deploy ~1 h before the start (`deploy4w.sh`), delete as soon as the results are downloaded (`DELETE=1 bash deploy4w.sh`): a min-instance bills for every second it exists |

Expected: ~37 samples per size. Every sample should be answered by the instance that has been
running since the deploy (the app reports `cold=true` only for the very first request of an
instance, which the warm-up absorbs).

### 10.2 What it adds

The latency and the cost of the always-warm side per size, measured, next to the scale-to-zero
numbers of the main run at the same idle tier (16 min): how much waiting the min-instance saves
per request, and what it costs, for S, M and L.

### 10.3 Rules (fixed before the always-warm run; `analyze4w.py`)

- **W1 - does always-warm remove the cold start?** For a size: yes if at least 95% of the
  successful samples are answered in under 1,000 ms and no sample had to wait for a new instance
  (`cold=true` with an instance younger than 60 s). Slow samples are listed with their instance.
- **W2 - saving per request:** median latency scale-to-zero (main run, 16 min tier) minus median
  latency always-warm, per size, and the ratio. Descriptive; the two runs are on different days
  in the same hours (limitation 10.5).
- **W3 - instances:** number of instances per always-warm service during the run (1 = the same
  instance all day; more = replaced by the platform), from the app and system logs.
- **W4 - cost:** from the billing export (`billing_query4w.sql`): min-instance vCPU-s and GiB-s
  (summed over the whole existence, because min-instance rows arrive in uneven hourly chunks, as
  in Tests 1 - 2), request-based part, gross EUR; price per second and a 30-day projection.
  Compared with scale-to-zero at the same request pattern: the main run's 10 h cost of the 16 min
  service x 72 = one request every 16 minutes for 30 days.

### 10.4 Plumbing

As in section 6: `app_size` = services file, request id echoed, client attempts = request log =
billed requests, config check shows min 1 / max 1 / 1 CPU / 2 GiB / no boost / right image.

### 10.5 Limitations of the addition

- Different day **and different hours** than the scale-to-zero run (evening / night instead of
  08:00 - 18:00 UTC; first planned for Monday 08:00 - 18:00 UTC, moved forward on 2026-11-20 to
  have the results sooner). Warm latency depended little on the time of day so far (Tests 1 - 2 at
  night: ~251 ms; Test 4 main run, 5 min tier, daytime: 231 - 254 ms), against cold-start
  differences of seconds, so W2 is not expected to change; it stays descriptive. Section 11.1 of the
  pilot showed service-to-service differences of ~0.4 s and the main run showed up to ~6 s between
  L services; this run has one service per size.
- Added after the main run's results were seen (see "Why this was added").

### 10.6 Cost estimate

3 x 1 vCPU + 2 GiB min-instance = 3 x 9.24e-06 EUR/s = ~EUR 0.10 per hour; ~11.5 h from deploy
to delete = **~EUR 1.15 gross**, plus the VM (~EUR 0.12). The min-instance part is probably not
covered by the free tier.

### 10.7 Run book

```bash
# --- Cloud Shell, shortly before the start (images exist; no build) ---
cd ~/cold-start-lab && git pull && cd "Test 4"
export PROJECT_ID=cold-start-lab REGION=europe-central2
bash deploy4w.sh                       # 3 services, min-instances 1, writes services4w.csv; note the deploy time
gcloud compute instances start coldstart-measurer --zone europe-central2-a
gcloud compute scp measure4.py services4w.csv coldstart-measurer:~ --zone europe-central2-a
gcloud compute ssh coldstart-measurer --zone europe-central2-a

# --- on the VM ---
export PROJECT_ID=cold-start-lab
python3 measure4.py --services services4w.csv --dry-run
nohup python3 measure4.py --services services4w.csv --duration-h 10 --start-at 2026-11-20T19:00Z \
  --out results4w.csv --meta run4w_meta.txt > measure4w.log 2>&1 &
disown

# --- after the run (Cloud Shell, in Test 4/) ---
gcloud compute scp coldstart-measurer:~/results4w.csv coldstart-measurer:~/run4w_meta.txt \
  coldstart-measurer:~/measure4w.log . --zone europe-central2-a
PREFIX=coldstart-t4w- OUT=logs4w START=2026-11-20T18:00:00Z END=2026-11-21T05:30:00Z bash collect_logs4.sh
DELETE=1 bash deploy4w.sh              # stop the min-instance bill right away
gcloud compute instances stop coldstart-measurer --zone europe-central2-a
# the next day:
bq query --nouse_legacy_sql --format=csv --max_rows=10000 "$(cat billing_query4w.sql)" > billing4w.csv
python3 analyze4w.py results4w.csv --zero results4.csv --logs logs4w \
  --billing billing4w.csv --zero-billing billing4.csv | tee analyze4w_output.txt
```

---

# Results (written 2026-11-21)

Sections 1 - 10 were written before the respective runs and are unchanged, apart from the status
line. The results follow the numbering of the rules: 5.x for the main run, W1 - W4 for the
always-warm addition. Output files: `analyze4_output.txt`, `analyze4w_output.txt`.

## 11. What was actually run

**Main run (scale-to-zero).** Built and deployed on 2026-11-18 at about 14:45 - 14:55 UTC
(`deploy_check4.txt`: all 18 services min 0, max 1, 1 vCPU, 2 GiB, no CPU boost, the right image).
Measured **2026-11-19 (Thursday), 08:00:00 - 17:58:33 UTC** (`run4_meta.txt`), started by
`--start-at` the day before; billing gate OK. 910 requests = 18 warm-ups + **892 samples**, all
answered on the first attempt (no retry was needed):

| tier | 5 min | 14 min | 15 min | 16 min | 18 min | 20 min |
|---|---|---|---|---|---|---|
| samples per service | 119 | 42 | 39 | 37 / 36 / 36 (S / M / L) | 32 | 29 |

As planned in section 3, apart from: the cold samples at 16 min are 37 / 36 / 36 instead of
37 each (the slower M and L starts push the next sample later, so one fewer fits into 10 h).
The deploy started one instance per service on 2026-11-18 (18 billed requests, 199 vCPU-s,
EUR 0.007), as the pilot had shown; it is outside the window and not counted.

**Always-warm addition.** Deployed 2026-11-20 18:34 UTC; measured **2026-11-20 19:00:00 -
2026-11-21 04:53:08 UTC** (`run4w_meta.txt`), 114 requests = 3 warm-ups + **111 samples**.
Deviation from 10.1: the services were deleted only at **2026-11-21 17:08 UTC**, about 12 h after
the end of the measurement (nobody was there in the morning), so they existed ~22.6 h instead of
~10.5 h. This changes the total cost of the run, not the price per second (W4).

## 12. Results of the main run (rules of section 5)

### 12.1 Cold rate per size and tier (5.3)

| tier | S cold | M cold | L cold |
|---|---|---|---|
| 5 min | 0 / 119 | 0 / 119 | 0 / 119 |
| 14 min | 0 / 42 | 0 / 42 | 0 / 42 |
| 15 min | 1 / 39 | 1 / 39 | 3 / 39 |
| 16 min | 37 / 37 | 36 / 36 | 36 / 36 |
| 18 min | 32 / 32 | 32 / 32 | 32 / 32 |
| 20 min | 29 / 29 | 29 / 29 | 29 / 29 |

Fisher S vs L: 14 min p = 1.0, 15 min p = 0.62, 16 min p = 1.0. **The boundary does not move with
the size**: up to 14 min always warm, from 16 min always cold, for every size.

The five cold samples at 15 min are all the same race: the request left the client at exactly
900.0 s after the previous response, and the previous instance logged its shutdown 0.05 - 0.2 s
*after* the request started. These are requests that arrived while the instance was being removed,
not an earlier teardown of a bigger application (S 1, M 1, L 3; not significant).

### 12.2 Cold-start latency per size (5.1)

| | S | M | L |
|---|---|---|---|
| cold samples (all tiers) | 99 | 98 | 100 |
| **median latency** | **3,021 ms** | **6,789 ms** | **12,791 ms** |
| p95 | 3,989 ms | 10,326 ms | 20,054 ms |
| median time to first byte | 2,871 ms | 6,640 ms | 12,650 ms |
| service medians 16 / 18 / 20 min | 3,051 / 3,003 / 3,021 | 6,769 / 6,735 / 7,025 | 16,933 / 10,540 / 12,417 |

| pair | median difference | Mann-Whitney (Holm) | all services separated | result |
|---|---|---|---|---|
| S - M | +3,768 ms | p < 0.0001 | yes | **shown** |
| S - L | +9,770 ms | p < 0.0001 | yes | **shown** |
| M - L | +6,002 ms | p < 0.0001 | yes | **shown** |

**The cold start grows with the size: ~3.0 s (S), ~6.8 s (M), ~12.8 s (L)**, by both conditions
of the rule: every service of a bigger size is slower than every service of a smaller one
(slowest M 7.0 s < fastest L 10.5 s).

The warm-ups (the first start of each service, ~17 h after the deploy) were slower again:
median 5.2 s (S), 12.2 s (M), 19.7 s (L).

### 12.3 Which part grows (5.2)

Medians of the cold samples, ms:

| | S | M | L |
|---|---|---|---|
| total latency | 3,021 | 6,789 | 12,791 |
| app `startup_ms` | 2,109 | 5,928 | 11,935 |
| - imports (FastAPI, pandas) | 1,260 | 1,192 | 1,311 |
| - data + SQLite (`init_ms`) | 846 | 3,390 | 4,218 |
| - scikit-learn import | - | 1,163 | 1,295 |
| - model load | - | 34 | 5,082 |
| request work (`work_ms`) | 111 | 88 | 109 |
| `platform_ms` (ttfb - startup - work) | 645 | 659 | 648 |

`platform_ms` L - S = +3 ms (rule: > 500 ms would point to the image / platform). **The growth
is in the application's own start-up**: the five-times-larger data (+2.5 - 3.4 s), the
scikit-learn import (+1.2 s) and, for L, loading the 500 MB model (+5.1 s). The part before the
application runs stays at ~650 ms although the image grows from 91 to 282 MiB (compressed,
Artifact Registry).

One qualification, beyond the rule: the model load includes reading the 504 MB file from the
container's file system, and on Cloud Run that took ~2.9x longer than locally (median 5.1 s vs
1.3 - 1.9 s), while the other phases took ~1.4 - 1.9x longer (imports S 1,260 vs ~760 ms; data
S 846 vs ~445 ms, M 3,390 vs ~2,380 ms, L 4,218 vs ~2,370 ms).
So the image size does show up, but inside `model_load_ms`, not in `platform_ms`: what costs time
is the large file the application reads at start-up, not the image as such. The spread between
the L services (section 15) comes from the same phase.

Local calibration vs Cloud Run (`startup_ms` medians): S 1.2 -> 2.1 s (x1.7), M 3.8 -> 5.9 s
(x1.6), L 5.7 -> 11.9 s (x2.1).

### 12.4 Teardown (5.3)

| | S | M | L |
|---|---|---|---|
| instances (exact join) | 105 | 104 | 106 |
| min / median / max | 900.1 / 905.4 / 910.1 s | 900.1 / 904.4 / 910.0 s | 900.1 / 905.3 / 910.0 s |
| below 900 s | 0 | 0 | 0 |

S vs M: -1.0 s (Holm p = 0.37), S vs L: -0.1 s (p = 0.86): **no shift**. The idle timeout is
the same ~900 - 910 s for every size, as in Test 3 (889 - 910 s) and the pilot (900 - 910 s).
No early teardown (< 800 s) occurred; every one of the 315 instances served at least one request
and logged a shutdown.

### 12.5 Warm requests (5.4)

5 min tier: S 254 ms, M 232 ms, L 231 ms (median; p95 271 / 249 / 247 ms). Largest difference
23 ms, below the 100 ms threshold: **warm requests cost the same for every size**. S is slightly
the slowest, and its `/api/report` work too (94 vs 72 / 70 ms) although the rows are the same;
this was also seen in the always-warm run (13) and is not explained.

### 12.6 Failures, memory and overlaps (5.6)

- **No HTTP 429, no 5xx, no connection error** in 892 samples (95% CI of the failure rate per size
  0 - 1.3%). The pilot settings (max-instances 1, 2 GiB, staggered starts) carried over without
  the Test 3 failure mode, also for the slowest size and also in the Test 3 peak hours.
- **Memory**: median after start-up S 176 MB, M 423 MB, L 1,389 MB; peak L 1,491 MB (73% of
  2 GiB). No instance without a shutdown line, no memory-related system log entry: no OOM.
- **Overlaps**: 5 / 10 / 11 cold samples (S / M / L) overlapped another cold start; without them
  the medians change by 0.1 / 0.2 / 0.0%, far below the 10% threshold.

### 12.7 Cost (5.5)

Billed requests = client attempts for **every one of the 18 services** (928 billed incl. the 18
deploy-day requests, which are outside the window). Run window 08:00 - 18:59 UTC, gross:

| | S | M | L |
|---|---|---|---|
| all six services, 10 h | EUR 0.0159 | EUR 0.0322 | EUR 0.0583 |
| vCPU-s per cold start (16 / 18 / 20 min) | 3.80 | 8.12 | 14.46 |
| median cold latency of the same samples | 3.02 s | 6.79 s | 12.78 s |
| EUR per 1,000 cold starts | 0.139 | 0.296 | 0.527 |

Total Cloud Run cost of the main run: **EUR 0.106 gross, EUR 0 net**. A cold start costs about
1.1 - 1.3x its latency in billed vCPU time (the instance is billed from its start, before the
request is answered), so **the cost per cold start grows with the size in the same proportion as
the cold-start time**, as expected in section 1.

## 13. Results of the always-warm addition (rules W1 - W4)

### W1 - the cold start is removed for every size

| | S | M | L |
|---|---|---|---|
| samples (16 min idle) | 37 | 37 | 37 |
| median / p95 / max | 266 / 297 / 297 ms | 208 / 219 / 221 ms | 211 / 221 / 232 ms |
| under 1,000 ms | 37 / 37 | 37 / 37 | 37 / 37 |
| waited for a new instance | 0 | 0 | 0 |

### W2 - what it saves per request (16 min tier)

| | scale-to-zero median | always-warm median | saved per request | ratio |
|---|---|---|---|---|
| S | 3,051 ms | 266 ms | **2.8 s** | 11x |
| M | 6,769 ms | 208 ms | **6.6 s** | 33x |
| L | 16,933 ms | 211 ms | **16.7 s** | 80x |

The L value uses the 16 min service, the slowest L service of the main run; with the median of all
L cold samples (12.8 s) the saving is ~12.6 s. Different days and hours (10.5): the scale-to-zero
side ran in the day, the always-warm side in the evening and night; the warm latencies (208 -
266 ms) are in the range of the main run's warm requests (231 - 254 ms).

### W3 - the same instance all the time

Each always-warm service had **one instance** from the deploy (2026-11-20 18:34) to the deletion
(2026-11-21 17:08), ~22.6 h, and it answered all 38 requests (warm-up + 37). The system log shows
one "Starting new instance" per service. Client attempts = request-log entries (114 / 114), no
foreign traffic.

### W4 - cost

*Pending: the billing export of 2026-11-21 was incomplete when this was written (last export
14:52 UTC, deletion 17:08 UTC). `billing_query4w.sql` -> `billing4w.csv`, then
`analyze4w.py ... --billing billing4w.csv --zero-billing billing4.csv`.*

Expected from the list price (Tests 1 - 2, Tier 2): min-instance 1 vCPU + 2 GiB =
9.24e-06 EUR/s = EUR 0.033 per hour = **~EUR 24 per 30-day month per service, the same for every
size**. Compared with scale-to-zero at the same request pattern (one request every 16 min, all
month; main-run cost x 72): S EUR 0.39, M EUR 0.81, L EUR 1.70 per month, i.e. the always-warm
instance costs ~62x (S), ~30x (M) and ~14x (L) more. The whole Cloud Run bill of 2026-11-20 and
2026-11-21 (until 14:52) shows EUR 0.57 + 1.23 gross, consistent with ~EUR 0.10 per hour for the
three services; net EUR 0 (credits).

## 14. Plumbing checks (sections 6 and 10.4)

| # | check | main run | always-warm |
|---|---|---|---|
| 1 | local calibration | pass (2026-11-18) | - |
| 2 | deployed config | pass, 18 / 18 (`deploy_check4.txt`) | pass, 3 / 3 |
| 3 | billing gate before the run | pass | pass |
| 4 | client = request log = billed requests | 910 = 910 = 910 (+18 deploy) | 114 = 114 (billing pending) |
| 5 | `app_size` = services file | 910 / 910 | 114 / 114 |
| 6 | instance join, teardown matched | 315 / 315 | 3 / 3 |

## 15. Findings not covered by the rules

- **L varies between services.** The three cold L services had medians of 16.9, 10.5 and 12.4 s
  (stable through the day), and all of the difference is in the model load (7.6, 2.9, 4.8 s).
  `coldstart-t4-l-18` alternated between two levels (10.0 - 10.8 s with a 2.6 - 3.0 s load, and
  12.4 - 13.4 s with a 4.7 - 5.6 s load). Reading a large file at start-up is the part of a cold
  start that depends most on where the instance lands. The size effect still holds (12.2), but a
  single-service measurement of a large application can be off by several seconds.
- **Cold starts at the boundary were slower.** The five race cold starts at 15 min (12.1) took
  4.5 s (S), 11.6 s (M) and 17.6 - 19.7 s (L), against 3.0 / 6.8 / 12.8 s at 16 - 20 min; the
  imports alone took 2.4 - 4.1 s instead of ~1.3 s. Five samples only; a plausible reading is
  that the new instance started while the old one was still shutting down.
- **S is ~20 - 55 ms slower when warm** than M and L (12.5, 13), with the same work.

## 16. Conclusions for the paper

1. **The cold start grows strongly with the size of the application:** median ~3.0 s (S),
   ~6.8 s (M), ~12.8 s (L), p95 up to 20 s. Every step is shown by the pre-registered rule.
2. **The growth is the application's own start-up work** (more data, a heavier library, a large
   model); the platform's share before the application runs stays at ~0.65 s for a 91 - 282 MiB
   image. A large file read at start-up is the slowest and most variable part on Cloud Run.
3. **The teardown does not depend on the size:** ~900 - 910 s after the last response for S, M
   and L; 14 min idle was always warm, 16 min always cold, 15 min a race at the edge.
4. **Cost of a cold start grows with the size** (3.8 / 8.1 / 14.5 vCPU-s, EUR 0.14 / 0.30 / 0.53
   per 1,000), but stays tiny; Test 4 cost EUR 0.11 gross for 892 samples.
5. **Always-warm removes the cold start for every size** (all 111 samples 0.2 - 0.3 s) and costs
   the same for every size (~EUR 24 per month at 1 vCPU + 2 GiB, W4 pending). So the bigger the
   application, the more the same money buys: 2.8 s per request for S, 6.6 s for M, 12.6 - 16.7 s
   for L at 16 min idle.
6. **No 429** in 1,003 samples under max-instances 1, 2 GiB and staggered starts, including the
   slowest size and the Test 3 peak hours; the Test 3 429s remain unexplained.

## 17. Limitations seen in the results

- One day per run; the always-warm side on a different day and at different hours (10.5).
- One always-warm service per size; the main run showed that L services can differ by ~6 s.
- The models are synthetic RandomForests (section 7); the size of the model file, not its kind,
  drives the load time here.
- Cost: hourly billing rows; the always-warm figures (W4) are list-price estimates until the
  export is complete.

## 18. Notes for the next run

- The measurement VM kept running from the deploy (2026-11-18) to 2026-11-21 (~EUR 0.23 per day for
  Compute Engine + ~EUR 0.06 for networking), and the always-warm services ran ~12 h past the
  end of their measurement (~EUR 1.2). Put the deletion and the VM stop into the same session as
  the download, or schedule them.
- `git pull` in Cloud Shell failed because files written there by the analysis had been committed
  from the local copy; move them aside first.
- Scripts committed from Windows lose the executable bit: run them with `bash script.sh`.
- `gcloud artifacts docker images list` prints the creation time in local time (CET), not UTC.
