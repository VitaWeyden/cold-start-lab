# Test 1 Report — Scale-to-Zero vs. Always-Warm Cold Start Measurement

**Project:** `cold-start-lab` — Cloud Native Architecture lab, scale-to-zero vs. always-warm Cloud Run comparison
**Author:** Zsófia Gergely
**Status:** Complete. Measurement VM stopped (not deleted) — kept for Test 2.

This report documents the first full measurement run end to end: the system under
test, the exact deployment configuration, the measurement methodology actually
executed, the resulting data, and the billing validation against real Google Cloud
charges. It is meant to stand on its own (for the paper's Methodology/Results
chapters) and as the baseline that Test 2 will be compared against.

---

## 1. Overview

| | |
|---|---|
| Question tested | Does `min-instances=0` (scale-to-zero) show materially worse cold-start latency than `min-instances=1` (always-warm), and at what idle duration does Cloud Run actually tear the zero-instance service down? |
| Measured app | FastAPI webshop analytics API (see §2) |
| Region | `europe-central2` (Warsaw) |
| Measurement window | 2026-10-02 20:12 → 2026-10-03 ~06:33 (local, UTC+2) — **≈10h 21m**, unattended |
| Idle tiers tested | 5, 15, 30, 60 minutes |
| Total requests recorded | 264 data rows (`results.csv`) |
| Headline finding | Scale-to-zero teardown threshold lies **between 15 and 30 minutes** for this configuration. Cold starts cost **3.2–5.9 s**, warm requests **~200–350 ms**. |
| Headline cost finding | Over the measurement window, the always-warm service billed **€0.26**; the scale-zero service billed **≈€0.00** (negligible). |

---

## 2. System Under Test

A mid-sized **webshop analytics API** (not a "hello world"), so a cold start pays for
real initialization work:

- **Stack:** FastAPI + Uvicorn, pandas, NumPy, SQLite (in-memory)
- **Startup work:** generates a synthetic dataset (200,000 orders, 5,000 products,
  seeded RNG) and loads it into an in-memory SQLite database with two indexes
  (`idx_orders_id`, `idx_products_category`)
- **Endpoints:**
  - `/` — light, no DB access
  - `/api/products?category=...&limit=...` — SQLite query
  - `/api/orders/{id}` — primary-key lookup
  - `/api/report` — heavy: pandas `groupby`/aggregation over all 200k orders
    (**this is the endpoint used for the full measurement**)
- Every response carries instance metadata instead of relying on client-side
  timing alone: `variant`, `revision`, `instance_id`, `cold` (is this the first
  request this instance has served), `request_count`, `instance_uptime_s`,
  `startup_ms` (process start → dataset ready), `init_ms` (dataset build alone),
  `work_ms` (server-side compute time for this request only)

### Deployment configuration

Both services were built from the **same container image** and differ in exactly
one setting:

| | `coldstart-scale-zero` | `coldstart-always-warm` |
|---|---|---|
| `min-instances` | 0 | 1 |
| `max-instances` | 1 | 1 |
| CPU | 1 vCPU | 1 vCPU |
| Memory | 1 GiB | 1 GiB |
| Concurrency | 80 | 80 |
| CPU boost | disabled (`--no-cpu-boost`) | disabled (`--no-cpu-boost`) |
| Region | `europe-central2` | `europe-central2` |
| Image | `shop-api:v1` | `shop-api:v1` (identical) |
| Auth | `--allow-unauthenticated` | `--allow-unauthenticated` |

`max-instances=1` on both sides means neither service ever scaled out
horizontally during the test — every measured difference is attributable to the
`min-instances` setting alone, not to concurrent-instance effects.

---

## 3. Measurement Environment

- **Client:** a dedicated `e2-micro` Compute Engine VM (`coldstart-measurer`),
  Debian 12, zone `europe-central2-a` — **same region as both Cloud Run
  services**, so network/TLS latency is small and roughly equal for both
  variants, and the measurement is not dependent on the operator's home network
  or a Cloud Shell session that can be dropped by inactivity.
- **Measurement tool:** `measure.py` (standard library only, no dependencies to
  install on the VM). Each timed request is a `urllib.request.urlopen()` call;
  `latency_ms` is wall-clock time for the full request/response round trip
  (includes TLS handshake). The JSON body's `cold`/`instance_id` fields are
  parsed and recorded alongside it, so a cold start is confirmed by the
  **server**, not inferred from a latency threshold.
- **Pairing:** for every idle-scenario repetition, both variants are requested
  **simultaneously** from two threads (`paired_request()`), so both experience
  the same idle gap and the same wall-clock conditions.

---

## 4. Methodology

### 4.1 Scenarios

| Scenario | What it measures |
|---|---|
| `idle_5min` | First request after 5 minutes of no traffic to either service |
| `idle_15min` | First request after 15 minutes of no traffic |
| `idle_30min` | First request after 30 minutes of no traffic |
| `idle_60min` | First request after 60 minutes of no traffic |
| `sequential` | 100 back-to-back requests (200 ms gap) once both instances are warm — the steady-state baseline |

For each idle tier, the idle period elapses once, then one *paired* request is
sent and repeated for N repetitions — i.e. the service goes idle again
immediately after being hit, so every repetition is an independent idle-then-hit
trial at that duration.

### 4.2 Exact command executed on the VM

Because `measure.py` appends to an existing CSV instead of overwriting it, the
four idle tiers and the sequential baseline were chained into one unattended,
background job so the whole run (~10h20m estimated) would survive the operator
closing the SSH session:

```bash
nohup bash -c '
python3 measure.py --urls-file urls.env --idle 5  --repeats 10 --sequential 0   --out results.csv &&
python3 measure.py --urls-file urls.env --idle 15 --repeats 10 --sequential 0   --out results.csv &&
python3 measure.py --urls-file urls.env --idle 30 --repeats 8  --sequential 0   --out results.csv &&
python3 measure.py --urls-file urls.env --idle 60 --repeats 3  --sequential 100 --out results.csv
' > measure.log 2>&1 &
disown
```

(`--path /api/report` is the script default, so it was not passed explicitly.)
The repeat counts were deliberately reduced at the longer tiers (10 → 8 → 3) to
keep the total run to one evening/overnight: 10×5min + 10×15min + 8×30min +
3×60min + 100 sequential ≈ 10h20m end to end.

- **Started:** 2026-10-02 18:12 UTC (20:12 local, Bratislava/Warsaw time)
- **Finished:** 2026-10-03 ~04:33 UTC (~06:33 local) — matches the
  `idle_60min` iteration 3 timestamp, the last idle-tier row in `results.csv`,
  followed immediately by the 100 sequential requests
- **Total wall-clock duration:** ≈10h21m, fully unattended, VM survived an SSH
  disconnect without interruption (`disown`)

### 4.3 Statistics

`analyze.py` groups `results.csv` by `(scenario, variant)` and reports `n`,
`cold_starts` (count of server-reported `cold=True`), `errors`, and
`median_ms` / `p95_ms` (linear interpolation) / `mean_ms` / `min_ms` / `max_ms`.

---

## 5. Results

Final output of `python3 analyze.py results.csv` (264 data rows → `summary.csv`):

```
    scenario       variant        n   cold_starts   errors   median_ms   p95_ms   mean_ms   min_ms   max_ms
  idle_15min   always-warm       10             0        0       250.5    271.2     252.5    236.7    272.1
  idle_15min    scale-zero       10             0        0       253.1    284.1     257.4    236.1    284.6
  idle_30min   always-warm        8             0        0       256.7    316.6     265.5    241.3    346.8
  idle_30min    scale-zero        8             8        0      3463.5   5787.4    4243.0   3225.1   5859.2
   idle_5min   always-warm       10             0        0       244.9    268.6     248.2    234.2    272.7
   idle_5min    scale-zero       10             1        0       247.3   2711.7     695.1    239.1   4720.7
  idle_60min   always-warm        3             0        0       266.6    282.8     271.3    262.6    284.6
  idle_60min    scale-zero        3             3        0      5228.2   5456.2    5290.5   5161.8   5481.6
  sequential   always-warm      100             0        0       208.1    223.1     210.6    201.2    263.9
  sequential    scale-zero      100             0        0       209.8    235.6     213.5    198.7    253.9
```

### 5.1 Cold-start rate by idle duration — the threshold finding

| Idle duration | scale-zero cold starts | Interpretation |
|---|---|---|
| 5 min | 1 / 10 (10%) | Only the very first repetition was cold (instance still warming up right after the pilot test); the other nine 5-minute gaps did **not** trigger a teardown |
| 15 min | 0 / 10 (0%) | No teardown observed at 15 minutes |
| 30 min | 8 / 8 (100%) | **Every** 30-minute gap triggered a full teardown and cold start |
| 60 min | 3 / 3 (100%) | Confirms the instance does not linger once past the threshold |

**Conclusion:** for this configuration (1 vCPU / 1 GiB, this application, this
region), Cloud Run's scale-to-zero idle timeout sits **somewhere between 15 and
30 minutes** — consistent with Google's own guidance that idle instances are
"usually" reclaimed within about 15 minutes, but not guaranteed at exactly that
mark. The always-warm service never showed a cold start in any scenario
(`cold_starts=0` throughout), as expected.

### 5.2 Latency: cold vs. warm

- **Warm requests** (any scenario, either variant, once an instance is up):
  **~200–350 ms** median/p95 — tight and consistent across all five scenarios.
- **Cold requests** (scale-zero, 30/60-min tiers): **3.2–5.9 s** median-to-max —
  roughly **13–24× slower** than warm. Sample raw rows (30-min tier):

  ```
  idle_30min,scale-zero,1,5859.24 ms,cold=True,instance_uptime_s=4.57
  idle_30min,scale-zero,3,3340.77 ms,cold=True,instance_uptime_s=2.42
  idle_30min,scale-zero,7,5573.51 ms,cold=True,instance_uptime_s=4.36
  idle_60min,scale-zero,1,5161.84 ms,cold=True,instance_uptime_s=3.97
  idle_60min,scale-zero,3,5481.56 ms,cold=True,instance_uptime_s=4.22
  ```

  The `instance_uptime_s` values (2.4–4.6 s) line up with the measured latency,
  confirming the delay really is container start + app initialization
  (dataset build + SQLite load), not a network fluke.
- **Sequential baseline:** once both instances are warm, the two variants are
  statistically indistinguishable (208.1 ms vs. 209.8 ms median, 223.1 ms vs.
  235.6 ms p95) — this is the expected "no difference once warm" control.

### 5.3 A resolved open question from the pilot test

The pre-full-run pilot (`OBSERVATIONS.md`, 2026-10-01) had observed ~5.2 s
latency spikes on the always-warm variant even with `cold=False`, hypothesized
to be Cloud Run's CPU-throttling-when-idle behavior affecting a technically-warm
instance. **This did not reproduce anywhere in the full run** — always-warm
stayed at 236–347 ms across all four idle tiers, including after a full 60
minutes of silence. The pilot spike is now best explained as an artifact of
measuring immediately after the service's very first few requests post-deploy,
not a general per-idle-gap phenomenon. This resolves the first open question
listed in `OBSERVATIONS.md`; the CPU-throttling nuance is still worth a short
mention in Limitations, but it is not a confound in the reported numbers.

---

## 6. Billing Validation

The GCP Billing report was checked the following day (reports can lag up to 24h)
using **Reports → Group by SKU**, filtered to the project and the measurement
window. Because both services have `max-instances=1`, each Cloud Run billing SKU
category maps 1:1 onto one of the two services:

| SKU | Usage | Maps to | Usage cost |
|---|---|---|---|
| Services **Min Instance** CPU Tier 2 | 40,854.6 s (≈11.35 h) | `coldstart-always-warm` — billed continuously while the min-instance exists | €0.13 |
| Services **Min Instance** Memory Tier 2 | 40,854.6 GiB·s | `coldstart-always-warm` | €0.13 |
| Services CPU Tier 2 (Request-based) | 79 s | `coldstart-scale-zero` — billed only while actually processing a request | ≈€0.00 |
| Services Memory Tier 2 (Request-based) | 79 GiB·s | `coldstart-scale-zero` | ≈€0.00 |
| Requests | 81 count | both (negligible at this volume) | €0.00 |

(The matching 79 s / 79 GiB·s pair for scale-zero confirms the 1 vCPU / 1 GiB
config exactly — 1 GiB·s per CPU-second.) All of the above was offset to a
€0.00 subtotal by free-trial credit ("Other savings"); the **usage cost** column
above is the real, gross cost the experiment generated, which is the number
that belongs in the paper.

**Result: €0.26 total for `always-warm` over ~11.35 h of continuous existence;
≈€0.00 for `scale-zero`.** Extrapolated to continuous operation:

```
€0.26 / 11.35 h ≈ €0.023/h ≈ €0.55/day ≈ €16.70/month (always-warm, continuous)
scale-zero: negligible at this traffic volume, by construction
```

### 6.1 Comparison with the `cost.py` model

`cost.py`'s price constants are the published **USD** list prices for a
**Tier 1** region (e.g. `europe-west1`); the actual deployment is in
`europe-central2`, a **Tier 2** region with slightly higher unit prices, and
the real bill is in EUR, not USD. `cost.py` now takes an optional `--eur-rate`
to convert its USD output at a flat rate for an easier side-by-side comparison
(GCP's actual invoice uses its own rate at billing time, so this stays an
approximation — state the rate and its date in the paper). Run with the
sequential-baseline mean latency (210 ms) as a representative `avg-ms`, 1 GiB
memory, and an indicative rate of 0.92 USD→EUR:

```
$ python3 cost.py --requests-per-month 100000 --avg-ms 210 --memory-gib 1.0 --eur-rate 0.92

always-warm (min=1)
  idle CPU (min-instance)          $   6.4275   (~EUR  5.9133)
  idle memory (min-instance)       $   6.4275   (~EUR  5.9133)
  TOTAL                            $  12.8550 / month   (~EUR 11.8266 / month)

scale-zero (min=0)
  TOTAL                            $   0.0000 / month   (100k req/month is inside the free tier)

Extra cost of the warm instance: $12.86 / month  (~EUR 11.83 / month, at rate 0.92)
```

Model estimate (**≈€11.83/month**, Tier-1 prices converted at an indicative
rate) vs. real extrapolated cost (**≈€16.70/month**, actual Tier-2 billing) are
in the same order of magnitude; the remaining gap is consistent with
`europe-central2` being a Tier 2 (pricier) region than the Tier 1 prices the
model uses — a reasonable cross-check, not an exact prediction. Before citing
either number in the paper, verify the current USD→EUR rate and Cloud Run
Tier 2 prices for the date of the measurement.

---

## 7. Limitations

- **Single region, single client location:** both Cloud Run services and the
  measuring VM are in `europe-central2`; results may differ in other regions or
  for clients farther from that region.
- **Small sample at the longer idle tiers:** 8 repetitions at 30 min, 3 at 60
  min (vs. 10 at 5/15 min), traded off to keep the whole run to one overnight
  session. The cold-start *rate* conclusion (0% → 100% between 15 and 30 min)
  is clear regardless, but the latency *distribution* at 60 min (p95, etc.) is
  based on only 3 points.
- **One application, one image:** a Python/FastAPI service with this dependency
  set and image size. A heavier runtime (JVM) or a much larger image would be
  expected to show a longer cold start; a minimal static binary, a shorter one.
  State the runtime and image size whenever comparing against published
  benchmarks.
- **Free-trial credit masks the Billing UI subtotal:** the €0.00 subtotal is
  not "free," it's trial credit absorbing the real usage cost; the "usage cost"
  / SKU breakdown above is the number that reflects actual resource
  consumption.
- **`cost.py`'s prices are Tier‑1, USD, point-in-time list prices** — verify
  against the current price list and note the date when citing it; treat it as
  a sanity-check model, not a billing prediction.
- **Controlled factors:** `max-instances=1` and `--no-cpu-boost` on both
  services, identical image/resources otherwise. No concurrent-request /
  scale-out behavior was tested (out of scope for this comparison).

---

## 8. Status and Next Steps

- `results.csv` (264 rows), `summary.csv`, `latency.png`, and this report
  constitute the Test 1 dataset.
- The measurement VM (`coldstart-measurer`, `europe-central2-a`) has been
  **stopped, not deleted** — it is not billing compute time while idle, and can
  be restarted (`gcloud compute instances start coldstart-measurer
  --zone=europe-central2-a`) when Test 2 is ready to run, without redoing the
  setup.
- Both Cloud Run services remain deployed as-is unless a Test 2 design requires
  changing them (e.g. a `--no-cpu-boost`/CPU-allocation variant, or measuring
  the light `/` endpoint instead of `/api/report` for comparison).
- Open design question carried into Test 2: which endpoint/traffic pattern to
  measure, and whether the finer boundary between 15 and 30 minutes (e.g. 20,
  25 min) is worth narrowing down, given how cleanly the 15→30 min jump already
  demonstrates the threshold.