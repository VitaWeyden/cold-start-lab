# Cloud Run: scale-to-zero vs. warm instance

Small-scale university lab experiment (Cloud Native Architecture) comparing Google
Cloud Run's `min-instances=0` (scale-to-zero) and `min-instances=1` (always-warm)
configurations: cold start latency, steady-state latency, and monthly cost.

This repository holds the code and scripts only. The raw measurement results,
the written report, and any screenshots of the Billing report are added once the
measurement has been run against a real Google Cloud project (see the
[Procedure](#procedure) section below).

Small-scale experiment: the same application with the same resources in two Cloud Run configurations.

The application is a mid-sized **webshop analytics API** (FastAPI, pandas, NumPy, SQLite). At startup it builds a 200,000-row dataset and loads it into an in-memory SQLite database, so a cold start pays for real initialisation work, and the container image is far larger than a "hello world".

| | scale-zero | always-warm |
|---|---|---|
| min-instances | 0 | 1 |
| max-instances | 1 | 1 |
| CPU / memory | 1 vCPU / 1 GiB | 1 vCPU / 1 GiB |
| image | same | same |

## Files

| File | Purpose |
|---|---|
| `app/main.py`, `app/data.py` | the measured application: FastAPI service + dataset generation / SQLite loading |
| `app/requirements.txt`, `app/Dockerfile` | pinned dependencies and the container image |
| `deploy.sh` | builds the image and deploys both services, URLs go to `urls.env` |
| `measure.py` | the measurement (first request after idle time + back-to-back requests) -> `results.csv` |
| `analyze.py` | median, p95, cold start count -> `summary.csv`, `latency.png` |
| `cost.py` | monthly cost estimate based on the price list |
| `cleanup.sh` | deletes the resources (mandatory when the measurements are done!) |
| `OBSERVATIONS.md` | dated log of things found while setting up and testing, feeds the Results/Limitations sections |
| `Test 1/Test1_report.md` | full methodology, results and billing validation for the completed first measurement run |
| `deploy3.sh`, `measure3.py`, `analyze3.py` | Test 3 (boundary): one service per idle tier, parallel measurement with real idle times, analysis incl. boundary bracket / logistic fit |
| `teardown_times.py`, `billing_query.sql` | Test 3: teardown moment from Cloud Logging, hourly per-service cost from the BigQuery billing export |

## Procedure

### 0. Prerequisites
- A Google Cloud project with billing enabled, `gcloud` authenticated (`gcloud auth login`)
- Python 3.10+ (`measure.py`, `analyze.py` and `cost.py` need no external packages; for the plot: `pip install matplotlib`; to run the app locally: `pip install -r app/requirements.txt`)

### 1. Local trial (no cloud needed)
```bash
pip install -r app/requirements.txt
cd app
VARIANT=scale-zero  python3 -m uvicorn main:app --port 8081 &
VARIANT=always-warm python3 -m uvicorn main:app --port 8082 &
cd ..
python3 measure.py --scale-zero-url http://localhost:8081 --warm-url http://localhost:8082 \
    --idle 0.05 --repeats 2 --sequential 20 --out local_test.csv
python3 analyze.py local_test.csv
```

#### Windows (PowerShell) version of the local trial
The commands above are for bash. On Windows, use a virtual environment (so the pinned versions do not change your global packages) and three terminals:
```powershell
# Terminal 0 (once, in the project folder)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r app/requirements.txt

# Terminal 1
cd app
$env:VARIANT = "scale-zero"
python -m uvicorn main:app --port 8081

# Terminal 2 (activate the venv here too)
cd app
$env:VARIANT = "always-warm"
python -m uvicorn main:app --port 8082

# Terminal 3 (activate the venv here too, project folder)
python measure.py --scale-zero-url http://localhost:8081 --warm-url http://localhost:8082 --idle 0.05 --repeats 2 --sequential 20 --out local_test.csv
python analyze.py local_test.csv
```
Use `python -m uvicorn` (not `uvicorn`), because the `uvicorn.exe` script is usually not on the PATH. `deploy.sh` and `cleanup.sh` are bash scripts: run them in **Google Cloud Shell**, WSL or Git Bash (Cloud Shell is the simplest, `gcloud` is already installed and authenticated there). Use `python3` instead of `python` when running the scripts in Cloud Shell.

### 2. Deployment
```bash
export PROJECT_ID=your-project-id
export REGION=europe-west1
./deploy.sh
```
If `--allow-unauthenticated` fails because of an organization policy, ask for an exception, or use `gcloud auth print-identity-token` (then `measure.py` needs an extra `Authorization` header).

### 3. Measurement
Run it from **Cloud Shell or from a VM in the same region as the services**, so that network latency is small and stable. (Running from a home network also works, but it must be stated in the paper.)

Each idle tier is its own `IDLE_MINUTES:REPEATS` pair, given with `--tier` (repeatable) — this is what controls both how often a ping happens and how long the whole run takes, so one command fully defines a run instead of needing several chained invocations. The script prints the planned wall-clock time before it starts anything, e.g. `Estimated total run time: ~15h 00m`, so you know what you're committing to before walking away. By default the heavy endpoint `/api/report` is requested (a pandas aggregation over 200,000 rows); pass `--path /` to compare against the light endpoint instead (available paths: `/` light, `/api/products?category=books&limit=20` SQLite query, `/api/orders/12345` primary-key lookup, `/api/report` heavy). A Cloud Shell session may be cut off by inactivity, so prefer a small Compute Engine VM (`e2-micro`) with `nohup ... & disown` or `tmux`. The CSV is written row by row, so the finished part survives an interruption, and `measure.py` appends rather than overwrites, so re-running with more tiers just adds more rows.

The old `--idle 5 20 --repeats 10` style (one shared repeat count for every idle value) still works and is handy for a quick local trial; `--tier` takes priority whenever both are given.

#### Test 1 (completed)

Found that the scale-to-zero teardown threshold for this configuration lies somewhere between 15 and 30 minutes of idleness — see `Test 1/Test1_report.md` for the full methodology, the exact (manually chained, pre-`--tier`) commands, the results and the billing validation. In short: `idle_5min` 1/10 cold, `idle_15min` 0/10 cold, `idle_30min` 8/8 cold, `idle_60min` 3/3 cold, over a ~10h21m unattended run (2026-10-02 18:12 → 2026-10-03 ~04:33 UTC). The single cold `idle_5min` sample is the first request of the run, i.e. an artifact of the long gap before it (corrected: 0/9, see Test 2 below).

#### Test 2 (completed)

Test 1 only bracketed the threshold (0% at 15 min, 100% at 30 min) without showing how the cold-start rate actually rises in between. Test 2 was built to trace that: drop the 5-minute tier (Test 1 showed nothing interesting happens there), keep 15 min (expect ~0% cold) and 30 min (expect 100% cold) as the two "obvious" ends, and add **two** tiers *inside* that window — 20 and 25 minutes — with equal repetitions, so the mixed cold-start rate at each has a usable 95% CI (Wilson score, computed by `analyze.py`) instead of being reported as a single step-function jump.

```bash
nohup python3 measure.py --urls-file urls.env \
    --tier 15:10 --tier 20:10 --tier 25:10 --tier 30:10 \
    --sequential 100 --out results.csv > measure2.log 2>&1 &
disown
```

All four tiers used 10 repetitions each, ~15h estimated total. Unlike Test 1, which started measuring right after redeploy, the always-warm service had already been running continuously since Test 1 ended — a Cloud Run min-instance does not stop just because no measurement is currently active. That gap matters for isolating Test 2's own billing cost; see "Test 2 billing correction" under [Analysis and cost](#4-analysis-and-cost) below. Known timestamps for that calculation:

| | UTC |
|---|---|
| Test 1 end | ~2026-10-03 04:33 |
| Test 2 start | ~2026-10-03 18:54 |
| Idle gap (Test 1 end → Test 2 start) | ~14h 21m (~51,660 s) |
| Test 2 end | read from the `Done ->` line in `Test 2/measure_2.log` |

**Result (`Test 2/results_2.csv`, `Test 2/summary_2.csv`):** 15 min 1/10 cold, 20 min 10/10, 25 min 10/10, 30 min 10/10 — the boundary is *not* between 25 and 30 min but between 15 and 20 min. Caveat: the single cold sample in the 15-minute tier is the first request after the ~14 h gap between the tests, so the instance was already gone for a reason unrelated to the 15-minute idle time; excluded, 15 min is **0/9** (and 0/19 together with Test 1). `python3 analyze3.py "Test 2/results_2.csv" --legacy-drop-first` reproduces the corrected numbers. The same applies to Test 1's first `idle_5min` sample (corrected: 0/9).

(`OBSERVATIONS.md` has the full dated log this summary and the billing-correction derivation below are drawn from.)

#### Test 3 (the boundary, 15–20 min in 1-minute steps)

Test 2 narrowed the teardown threshold to (15, 20] minutes. Test 3 resolves it with one scale-to-zero service **per idle tier** (`coldstart-idle-15` … `coldstart-idle-20`), all measured **in parallel**, so every tier sees the same hours of the same night and the whole run takes about as long as the longest tier (20 samples × 20 min ≈ 7 h) instead of the sum of the tiers.

What changed compared with Test 2:

- **Warm-up request first** (`scenario=warmup`, never analysed), so every sample is a genuine "X minutes after a known previous request" — this removes the first-sample artifact above.
- **Real idle time is measured**, not assumed: `actual_idle_s` is the monotonic-clock time between the end of the previous response and the start of the next request.
- **Latency split** into DNS / TCP / TLS / time-to-first-byte / body (IPv4 only), next to the server-reported `startup_ms`, `init_ms`, `work_ms`.
- **Teardown moment from Cloud Logging** (`teardown_times.py`): for every cold sample, how long after the last request Cloud Run sent SIGTERM (uvicorn logs `Shutting down`). Pilot this once on real logs before relying on it.
- **Cost per service per hour** from the Billing export to BigQuery (`billing_query.sql`); the services carry the labels `experiment=boundary,idle_min=N`. No idle-gap correction is needed, since scale-to-zero billing is request-based.
- **Analysis** (`analyze3.py`): per-tier cold rate with Wilson CI; if the outcomes do not overlap, the boundary is reported as the bracket between the longest idle that stayed warm and the shortest that was cold (a logistic MLE does not exist for a perfect step); if they overlap, a logistic fit gives the 50% point with a bootstrap CI.

```bash
./deploy3.sh                                  # six services, copies image/cpu/memory from coldstart-scale-zero, writes services3.csv
python3 measure3.py --services services3.csv --dry-run
nohup python3 measure3.py --services services3.csv --reps 20 --out results3.csv > measure3.log 2>&1 &
disown
python3 analyze3.py results3.csv
```

Nothing else may send requests to these services during the run (browser tabs, uptime checks), otherwise the idle time is not what the CSV says.

### 4. Analysis and cost
```bash
python3 analyze.py results.csv
python3 cost.py --requests-per-month 100000 --avg-ms 70 --memory-gib 1.0 --eur-rate 0.92
```
`analyze.py` now also reports `cold_rate_pct` with a 95% (Wilson score) confidence interval per scenario — the number to quote for a tier that sometimes restarts and sometimes doesn't, instead of just the raw count. `cost.py`'s optional `--eur-rate` converts its USD estimate to EUR at a flat rate you supply, for an easier side-by-side with the EUR Billing report (see `Test 1/Test1_report.md` §6.1 for the caveats on this conversion).

#### Test 2 billing correction

Test 1's cost could be read straight off the Billing Reports delta, because its measurement window started right after redeploy — the delta *was* the measurement. Test 2 can't use that shortcut: the always-warm service kept billing through the ~14h21m idle gap between the two tests (see the Test 2 timestamps table above), so the raw Min-Instance-seconds delta between "end of Test 1" and "end of Test 2" covers three periods, not just Test 2 — the idle gap, Test 2's own run, and whatever further gap passes until the Billing report is actually checked (it lags up to 24h).

This is not a problem in practice, because Cloud Run's min-instance billing is a flat rate per elapsed wall-clock second — it doesn't matter whether the instance is idle or actually serving a request. That means the idle-gap portion can be subtracted out arithmetically, using the real per-second rate already derived from Test 1's billing validation (`Test 1/Test1_report.md` §6: €0.26 / 40,854.6 s ≈ **€0.00000636/s**, for 1 vCPU + 1 GiB, continuous, Tier 2 region pricing), instead of needing hour-level filtering in the Billing Reports UI (which only supports day-level granularity anyway):

```
predicted_idle_gap_cost  = idle_gap_seconds  x 0.00000636 EUR/s
predicted_test2_cost     = test2_duration_s  x 0.00000636 EUR/s
predicted_total_delta    = predicted_idle_gap_cost + predicted_test2_cost
```

Once Test 2's billing delta is visible (usually ~24h after the window closes), compare `predicted_total_delta` against the actual Min-Instance-seconds delta in Billing → Reports → Group by SKU, using Test 1's end-state snapshot as the baseline (40,854.6 s / 79 s / 81 count, confirmed by screenshot on 2026-10-03). If they match within rounding, subtracting `predicted_idle_gap_cost` from the real total delta gives a clean, isolated Test-2-only cost figure, safe to report alongside Test 1's €0.26 number. `scale-zero` needs no such correction — its billing is request-based, so it only accrues during the two measurement windows themselves, never during the gap between them.

#### BigQuery billing export (Test 3, one-time setup)

The Billing Reports UI only has day-level granularity. The standard usage cost export to BigQuery gives hourly rows per SKU and per label, so the cost of each Test 3 service can be read separately. It is **not backfilled**: enable it *before* the measurement starts, and expect the first rows a few hours later.

```bash
gcloud services enable bigquery.googleapis.com
bq --location=EU mk --dataset $PROJECT_ID:billing_export
```

Then in the Cloud Console: Billing → your billing account → Billing export → BigQuery export → *Standard usage cost* → Edit settings → pick the project and the `billing_export` dataset → Save (needs the Billing Account Administrator role). After the run, open BigQuery, paste `billing_query.sql` (replace `PROJECT` with your project id) and run it.

Then check the **Billing report** (Billing -> Reports) and compare the estimate with the actual usage. The report can lag by up to 24 hours, so check it the day after the measurement, not right away.

In the Reports table, filter by **Project** (your project) and set the **Time range** to cover the measurement period. Read the **"Usage cost"** column, not the "Subtotal" column: if you are still inside the free-trial credit, "Other savings" will cancel the usage cost down to a €0.00 subtotal, same as in the screenshot we looked at for the other project. The subtotal is what you actually pay (nothing, while on the trial); the usage cost is the real, gross cost the resources generated, and that is the number to compare against `cost.py`'s estimate in the paper. Break it down further by **Service** (Cloud Run, Artifact Registry, Cloud Build) if the project has other charges mixed in, so the comparison isolates this experiment.

### 5. Cleanup
```bash
./cleanup.sh
```

## Notes for the paper

- **Cold starts are reported by the server, not only inferred from timing.** Every response contains a `cold` field (is this the first request on this instance) and an `instance_id`. In the analysis, look at the `cold=True` and `cold=False` rows separately.
- **5 minutes of idleness may not shut the instance down.** Cloud Run usually stops a zero-traffic instance after a few minutes up to about 15 minutes, and this is not guaranteed. If the 5-minute scenario shows few `cold=True` rows, that is a result in itself, do not "fix" it.
- **In the `sequential` series the scale-zero instance is already warm**, so the two variants will be close. This is expected, it is the baseline for the "normal" latency.
- **Client-side measurement**: `latency_ms` includes the network and the TLS handshake. The server-side `work_ms` (in the response) covers only the computation. State the limitations that follow from the small scale (one region, one application, one client location, few samples) in the Limitations section, as the abstract says.
- **Startup time is visible in the response**: `startup_ms` (process start until the dataset is loaded) and `init_ms` (the data loading alone) are included in every response, so you can relate the measured cold start latency to the application's own initialisation time.
- **Controlled factors**: `max-instances=1`, `--no-cpu-boost` and identical image/resources on both sides. If you change this, justify it in the paper.
- **Cost**: the prices in `cost.py` are starting values. Check the current price list and note the date.
- **Comparison with published measurements**: look for cold start measurements (e.g. Google documentation, community benchmarks, blog posts) with the same runtime (Python, small image) and compare against them. A Java service or an image with heavier dependencies would give a longer cold start, a static binary a shorter one, so mention the runtime and image size when you compare.

## Repository hygiene

`.gitignore` excludes everything that is either regenerated by the scripts or
specific to one run: `urls.env` (live service URLs), the measurement CSVs,
`latency.png`, `measure*.log`, `services3.csv`, local virtual environments, and the usual
Python/OS/editor clutter. Nothing in this repository requires a secret file or
API key — `deploy.sh` and `cleanup.sh` use your already-authenticated `gcloud`
session (`gcloud auth login`), not a key file. If you ever add a service
account key for automation, never commit it; `.gitignore` already blocks
`*-key.json` and `service-account*.json` as a safety net.

## License

MIT License - see [LICENSE](./LICENSE). The code may be reused and adapted
with attribution; it does not cover the written report itself.

## Author

Zsófia Gergely - university lab project
