# Test 4 - how does the size of the application change cold starts, teardown and cost?

Status: **planned, not run yet.** Written before the run, so the analysis rules in section 5
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
