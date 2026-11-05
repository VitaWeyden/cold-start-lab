#!/usr/bin/env python3
"""Exact cost of Tests 1-3 from the BigQuery billing export (hourly rows, EUR).

  python3 billing/billing_windows.py > billing/billing_summary.txt

Input: billing/billing_hourly_2026-10.csv, exported from the standard usage cost table with
the query at the end of this docstring, plus the client CSVs of Tests 1-3 (for the request
counts and the measurement windows). Standard library only.

How the export behaves (checked on this data):
  * It IS backfilled: switched on in late October, it contains usage from 2026-09-01 on.
  * Request-based rows (CPU / memory while requests are served, request counts) land in the
    hour they happen: the billed request count matches the client CSV hour by hour in Tests 1
    and 2 (264/264, 282/282).
  * Min-instance rows are reported in uneven chunks (hours with ~2,400 s next to hours with
    ~4,800 s, a few > 3,600 s), so a single hour, or a window that starts or ends inside an
    hour, cannot be read off directly. Over long spans they add up to the wall-clock time.
    The always-warm cost of a window is therefore: wall-clock seconds x the exact per-second
    price from the export (which is the same in every row).
  * cost = gross usage cost; credits = the free-trial credit that cancels it (net 0).

Query used for the CSV (Cloud Shell):
  SELECT FORMAT_TIMESTAMP("%Y-%m-%dT%H:00Z", usage_start_time) AS hour_utc,
    service.description AS service, sku.description AS sku,
    (SELECT value FROM UNNEST(labels) WHERE key = "experiment") AS experiment,
    (SELECT value FROM UNNEST(labels) WHERE key = "idle_min") AS idle_min,
    usage.pricing_unit AS unit, ROUND(SUM(usage.amount_in_pricing_units), 6) AS amount,
    currency, ROUND(SUM(cost), 8) AS cost,
    ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 8) AS credits
  FROM `cold-start-lab.billing_export.gcp_billing_export_v1_015D6E_D0CFC3_EA1613`
  WHERE usage_start_time >= "2026-10-01" AND usage_start_time < "2026-10-17"
  GROUP BY 1, 2, 3, 4, 5, 6, 8 ORDER BY 1, 2, 3
"""
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSV = os.path.join(HERE, "billing_hourly_2026-10.csv")

SKU = {
    "req_cpu": "Services CPU Tier 2  (Request-based billing)",
    "req_mem": "Services Memory Tier 2 (Request-based billing)",
    "min_cpu": "Services Min Instance CPU Tier 2 (Request-based billing)",
    "min_mem": "Services Min Instance Memory Tier 2 (Request-based billing)",
    "requests": "Requests",
}
# Measurement windows (UTC), from the client logs / CSVs
T1 = ("2026-10-02T18:12:00", "2026-10-03T04:34:11")   # measure_1.log, results_1.csv
T2 = ("2026-10-03T18:54:00", "2026-10-04T09:57:47")   # README timestamps, results_2.csv
T3 = ("2026-10-12T12:22:48", "2026-10-12T19:05:39")   # Test3_report.md
T1_DEPLOY_SPAN_S = 40854.6                            # always-warm existence in Test1_report.md §6


def secs(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


rows = list(csv.DictReader(open(CSV)))
for r in rows:
    r["amount"], r["cost"] = float(r["amount"]), float(r["cost"])


def sel(lo, hi, **eq):
    """Rows with lo <= hour < hi (hour strings 'YYYY-MM-DDTHH'), matching the given fields."""
    return [r for r in rows if lo <= r["hour_utc"][:13] < hi and all(r[k] == v for k, v in eq.items())]


def tot(rs, sku_key=None, what="amount"):
    return sum(r[what] for r in rs if sku_key is None or r["sku"] == SKU[sku_key])


def eur(x):
    return f"EUR {x:.4f}"


# ---------------------------------------------------------------- unit prices
print("1. Exact unit prices in the export (EUR, gross, every row has the same price)\n")
price = {}
for k, s in SKU.items():
    rs = [r for r in rows if r["sku"] == s and r["amount"] > 0]
    a, c = sum(r["amount"] for r in rs), sum(r["cost"] for r in rs)
    price[k] = c / a if a else 0.0
    print(f"  {s:<62} {price[k]:.4e} per {rs[0]['unit'] if rs else '?'}")
warm_rate = price["min_cpu"] + price["min_mem"]  # 1 vCPU + 1 GiB min instance, per second
print(f"\n  always-warm (1 vCPU + 1 GiB min instance): {warm_rate:.4e} EUR/s = "
      f"EUR {warm_rate * 3600:.4f}/h = EUR {warm_rate * 86400:.4f}/day = EUR {warm_rate * 30 * 86400:.2f}/30-day month")
tier1_usd = {"req_cpu": 0.000024, "req_mem": 0.0000025, "min_cpu": 0.0000025, "min_mem": 0.0000025}
print("  ratio to the Tier 1 USD list price: " +
      ", ".join(f"{k} {price[k] / v:.3f}" for k, v in tier1_usd.items()) +
      "  (= 1.4 Tier 2 multiplier x ~0.878 EUR per USD)")

# ---------------------------------------------------------------- min-instance chunks
print("\n2. Min-instance rows come in uneven chunks (seconds reported per hour, always-warm)\n")
per_hour = defaultdict(float)
for r in rows:
    if r["sku"] == SKU["min_cpu"]:
        per_hour[r["hour_utc"]] += r["amount"]
vals = [v for v in per_hour.values() if v > 0]
print(f"  {len(vals)} hours with min-instance usage; per hour min {min(vals):.0f} s, max {max(vals):.0f} s; "
      f"{sum(v > 3600 for v in vals)} hours report more than 3,600 s")
life = sel("2026-10-02T17", "2026-10-04T16")
print(f"  always-warm life 2026-10-02 17h - 10-04 15h: {tot(life, 'min_cpu'):,.1f} s billed "
      f"= {tot(life, 'min_cpu') / 3600:.2f} h (slides: 167,813 s)")

# ---------------------------------------------------------------- per test
cl1 = list(csv.DictReader(open(os.path.join(ROOT, "Test 1", "results_1.csv"))))
cl2 = list(csv.DictReader(open(os.path.join(ROOT, "Test 2", "results_2.csv"))))
cl3 = list(csv.DictReader(open(os.path.join(ROOT, "Test 3", "results3.csv"))))


def req_block(name, lo, hi, client_rows, tcol):
    rs = sel(lo, hi, service="Cloud Run")
    rs = [r for r in rs if not r["idle_min"]] if name != "Test 3" else rs
    billed = tot(rs, "requests")
    client = sum(1 for r in client_rows if lo <= r[tcol][:13] < hi)
    cpu, c_cpu, c_mem = tot(rs, "req_cpu"), tot(rs, "req_cpu", "cost"), tot(rs, "req_mem", "cost")
    return billed, client, cpu, c_cpu + c_mem


print("\n3. Per test\n")
for name, (a, b), lo, hi, cl, tcol in (
        ("Test 1", T1, "2026-10-02T18", "2026-10-03T05", cl1, "timestamp_utc"),
        ("Test 2", T2, "2026-10-03T19", "2026-10-04T10", cl2, "timestamp_utc")):
    s = secs(a, b)
    billed, client, cpu, c_req = req_block(name, lo, hi, cl, tcol)
    print(f"  {name}: {a} -> {b} UTC = {s:,.0f} s ({s / 3600:.2f} h)")
    print(f"    always-warm (min instance, wall clock x price): {eur(s * warm_rate)}")
    print(f"    scale-zero (request-based CPU + memory, hours {lo[11:]}h-{int(hi[11:]) - 1:02d}h): "
          f"{cpu:.1f} vCPU-s, {eur(c_req)}")
    print(f"    requests billed {billed:.0f} vs client rows {client}")
    if name == "Test 1":
        print(f"    always-warm over its {T1_DEPLOY_SPAN_S:,.1f} s existence in the Test 1 report: "
              f"{eur(T1_DEPLOY_SPAN_S * warm_rate)} (report: EUR 0.26 from the rounded Reports view)")
pre = sel("2026-10-03T18", "2026-10-03T19", service="Cloud Run")
print(f"  Before Test 2 (18h, before the 18:54 start): {tot(pre, 'requests'):.0f} requests, "
      f"{tot(pre, 'req_cpu'):.1f} vCPU-s - checks before the run, not part of Test 2")
gap = secs(T1[1], T2[0])
print(f"  Gap Test 1 end -> Test 2 start: {gap:,.0f} s -> always-warm {eur(gap * warm_rate)} "
      f"(README correction rate 0.00000636 EUR/s would give {eur(gap * 0.00000636)})")
both = secs(T1[0], T2[1])
print(f"  Test 1 start -> Test 2 end incl. the gap: {both:,.0f} s -> {eur(both * warm_rate)}")

# Test 3 per service
print("\n  Test 3 (hours 12h-19h on 2026-10-12), per service label idle_min:")
t3 = sel("2026-10-12T12", "2026-10-12T20", service="Cloud Run")
by = defaultdict(list)
for r in t3:
    by[r["idle_min"] or "(no label)"].append(r)
c3 = Counter(r["idle_min"][:2] for r in cl3)
c3_429 = Counter(r["idle_min"][:2] for r in cl3 if r["http_status"] == "429")
print(f"    {'service':<12} {'billed req':>10} {'client':>7} {'429':>4} {'vCPU-s':>8} {'cost EUR':>10}")
for k in sorted(by):
    rs = by[k]
    cost = sum(r["cost"] for r in rs)
    print(f"    {k:<12} {tot(rs, 'requests'):>10.0f} {c3.get(k, 0):>7} {c3_429.get(k, 0):>4} "
          f"{tot(rs, 'req_cpu'):>8.1f} {cost:>10.5f}")
lab = [r for r in t3 if r["idle_min"]]
print(f"    labelled services total: {tot(lab, 'requests'):.0f} requests, {tot(lab, 'req_cpu'):.1f} vCPU-s, "
      f"{eur(sum(r['cost'] for r in lab))}")
print(f"    unlabelled in the 12h hour: {tot(by['(no label)'], 'requests'):.0f} requests, "
      f"{tot(by['(no label)'], 'min_cpu'):.1f} s of min instance")

# hour-by-hour request reconciliation for Test 3
print("\n  Test 3, hours where billed requests != client attempts (per service):")
bh, chh = Counter(), Counter()
for r in t3:
    if r["sku"] == SKU["requests"]:
        bh[(r["hour_utc"][11:13], r["idle_min"] or "-")] += r["amount"]
for r in cl3:
    chh[(r["request_start_utc"][11:13], r["idle_min"][:2])] += 1
for k in sorted(set(bh) | set(chh)):
    if bh[k] != chh[k]:
        cpu = tot([r for r in t3 if r["hour_utc"][11:13] == k[0] and (r["idle_min"] or "-") == k[1]], "req_cpu")
        print(f"    {k[0]}h  {k[1]:<3} billed {bh[k]:.0f}  client {chh[k]}  ({cpu:.1f} vCPU-s billed)")

# ---------------------------------------------------------------- VM
print("\n4. Measurement VM (Compute Engine + Network Intelligence Center), not experiment cost\n")
for name, lo, hi in (("Tests 1-2 period (10-02 17h - 10-04 15h)", "2026-10-02T17", "2026-10-04T16"),
                     ("Test 3 run (10-12 12h - 19h)", "2026-10-12T12", "2026-10-12T20"),
                     ("idle after Test 3 (10-12 20h - deletion)", "2026-10-12T20", "2026-10-17T00")):
    rs = [r for r in sel(lo, hi) if r["service"] in ("Compute Engine", "Networking")]
    print(f"  {name:<44} {eur(sum(r['cost'] for r in rs))}")
allc = defaultdict(float)
for r in rows:
    allc[r["service"]] += r["cost"]
print("\n5. Whole project 2026-10-01 - 10-16 by service (gross): " +
      ", ".join(f"{k} EUR {v:.2f}" for k, v in sorted(allc.items(), key=lambda kv: -kv[1]) if v >= 0.005) +
      f"; total EUR {sum(allc.values()):.2f}")