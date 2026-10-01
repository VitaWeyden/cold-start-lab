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
```bash
nohup python3 measure.py --urls-file urls.env --idle 5 20 --repeats 10 --sequential 100 --out results.csv > measure.log 2>&1 &
```
By default the heavy endpoint `/api/report` is requested (a pandas aggregation over 200,000 rows). To compare with the light endpoint, run the measurement a second time with `--path / --out results_light.csv` (this needs another 4 hours, or use fewer repetitions), and analyze it with `python3 analyze.py results_light.csv`. Available paths: `/` (light), `/api/products?category=books&limit=20` (SQLite query), `/api/orders/12345` (primary-key lookup), `/api/report` (heavy).
Duration: 10 x (5 + 20) minutes = about **4 hours 10 minutes**, plus the back-to-back requests. A Cloud Shell session may be cut off by inactivity, so prefer a small Compute Engine VM (`e2-micro`) with `nohup` or `tmux`. The CSV is written row by row, so the finished part survives an interruption.

### 4. Analysis and cost
```bash
python3 analyze.py results.csv
python3 cost.py --requests-per-month 100000 --avg-ms 70
```
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
`latency.png`, `measure.log`, local virtual environments, and the usual
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