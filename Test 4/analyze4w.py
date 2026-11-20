#!/usr/bin/env python3
"""Test 4 addition: always-warm (min-instances=1) vs scale-to-zero, per size, at 16 min idle.

Implements Test4_report.md section 10 (rules fixed before the always-warm run).
Standard library only; the helpers come from analyze4.py (same folder).

  python3 analyze4w.py results4w.csv --zero results4.csv
  python3 analyze4w.py results4w.csv --zero results4.csv --logs logs4w \
      --billing billing4w.csv --zero-billing billing4.csv

  1. Plumbing: app_size, request id echo, failures.                        [10.4]
  2. Always-warm latency per size; samples that waited for a new instance. [10.3 W1]
  3. Always-warm vs scale-to-zero (Test 4, same size, 16 min tier).        [10.3 W2]
  4. Instances of the always-warm services (replacements) from the logs.   [10.3 W3]
  5. Cost: min-instance + request-based, per size; 30-day projection vs
     scale-to-zero with the same request pattern.                          [10.3 W4]
"""
import argparse
import csv
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze4 import SIZES, is_ok, load_json, med, num, pct, table, ts, vals  # noqa: E402

TIER = 16.0
FAST_MS = 1000          # W1: a warm answer is below this
NEW_INSTANCE_S = 60     # W1: a request whose instance is younger than this waited for a start
MONTH_S = 30 * 24 * 3600


def first_attempts(path, tier=TIER):
    rows = list(csv.DictReader(open(path)))
    idle = [r for r in rows if r["scenario"].startswith("idle_") and r["attempt"] == "1"
            and float(r["idle_min"]) == tier]
    return rows, idle


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="results4w.csv (always-warm run)")
    ap.add_argument("--zero", required=True, help="results4.csv (Test 4 scale-to-zero run)")
    ap.add_argument("--logs", help="prefix of the always-warm log files, e.g. logs4w")
    ap.add_argument("--billing", help="billing4w.csv (billing_query4w.sql)")
    ap.add_argument("--zero-billing", help="billing4.csv (billing_query4.sql)")
    a = ap.parse_args()

    rows, warm = first_attempts(a.csv)
    _, zero = first_attempts(a.zero)
    sizes = [s for s in SIZES if any(r["size"] == s for r in rows)]
    t = sorted(ts(r["request_start_utc"]) for r in rows)
    print(f"{a.csv}: {len(rows)} rows, {len(warm)} measured samples at {TIER:g} min, sizes {sizes}")
    print(f"client window (UTC): {t[0]:%Y-%m-%d %H:%M:%S} -> {t[-1]:%H:%M:%S}")

    # 1. plumbing ---------------------------------------------------------------------------
    print("\n1. Plumbing  [10.4]")
    ok_all = [r for r in rows if is_ok(r)]
    print(f"  app_size = services file size: {sum(r.get('app_size') == r['size'] for r in ok_all)}/{len(ok_all)}")
    print(f"  request id echoed: {sum(r.get('echo_request_id') == r['request_id'] for r in ok_all)}/{len(ok_all)}")
    fails = [r for r in rows if not is_ok(r)]
    print(f"  failed attempts: {len(fails)} " + str(dict(Counter(str(r['http_status']) or 'ERR' for r in fails))))

    # 2. always-warm latency -------------------------------------------------------------------
    print(f"\n2. Always-warm latency at {TIER:g} min idle (successful first attempts)  [10.3 W1]")
    w1, out = {}, []
    for s in sizes:
        v = [r for r in warm if r["size"] == s and is_ok(r)]
        lat = vals(v, "latency_ms")
        fast = sum(1 for x in lat if x < FAST_MS)
        waited = [r for r in v if str(r["cold"]) == "True"
                  and (num(r["instance_uptime_s"]) or 0) < NEW_INSTANCE_S]
        flagged = sum(1 for r in v if str(r["cold"]) == "True")
        w1[s] = bool(lat) and fast >= 0.95 * len(lat) and not waited
        out.append([s, len(v), med(lat), pct(lat, 0.95), max(lat, default=""), f"{fast}/{len(lat)}",
                    flagged, len(waited), "yes" if w1[s] else "NO"])
    table(out, ["size", "n", "median_ms", "p95_ms", "max_ms", f"below_{FAST_MS}ms", "cold_flag",
                "waited_for_start", "cold_start_removed"])
    slow = [r for r in warm if is_ok(r) and (num(r["latency_ms"]) or 0) >= FAST_MS]
    for r in slow[:15]:
        print(f"    slow: {r['request_start_utc'][11:19]} {r['service']} {r['latency_ms']} ms cold={r['cold']} "
              f"uptime={r['instance_uptime_s']} s inst={r['instance_id']}")

    # 3. vs scale-to-zero -------------------------------------------------------------------
    print(f"\n3. Always-warm vs scale-to-zero (Test 4 main run), {TIER:g} min tier  [10.3 W2]")
    out = []
    for s in sizes:
        lw = vals([r for r in warm if r["size"] == s and is_ok(r)], "latency_ms")
        lz = vals([r for r in zero if r["size"] == s and is_ok(r)], "latency_ms")
        if lw and lz:
            mw, mz = statistics.median(lw), statistics.median(lz)
            out.append([s, len(lz), round(mz, 1), pct(lz, 0.95), len(lw), round(mw, 1), pct(lw, 0.95),
                        round(mz - mw, 1), round(mz / mw, 1)])
    table(out, ["size", "n_zero", "zero_median_ms", "zero_p95_ms", "n_warm", "warm_median_ms", "warm_p95_ms",
                "saved_per_request_ms", "zero_vs_warm_x"])
    print("  (different days: scale-to-zero 2026-11-19, always-warm on the date of this run, same hours)")

    # 4. instances -------------------------------------------------------------------------------
    if a.logs:
        print("\n4. Instances of the always-warm services  [10.3 W3]")
        applog = load_json(f"{a.logs}_app.json") or []
        syslog = load_json(f"{a.logs}_system.json") or []
        inst = defaultdict(lambda: {"service": "", "start": None, "shutdown": None, "served": 0})
        for e in applog:
            iid = e.get("labels", {}).get("instanceId", "")
            if not iid:
                continue
            d = inst[iid]
            d["service"] = e["resource"]["labels"].get("service_name", "")
            j = e.get("jsonPayload") or {}
            if j.get("event") == "instance_start":
                d["start"] = ts(j.get("process_start_utc"))
            elif j.get("event") == "instance_shutdown":
                d["shutdown"] = ts(e.get("timestamp"))
            elif j.get("event") == "request":
                d["served"] += 1
        out = []
        for iid, d in sorted(inst.items(), key=lambda kv: (kv[1]["service"], kv[1]["start"] or t[0])):
            out.append([d["service"], iid[-12:], f"{d['start']:%m-%d %H:%M:%S}" if d["start"] else "",
                        f"{d['shutdown']:%m-%d %H:%M:%S}" if d["shutdown"] else "-", d["served"]])
        table(out, ["service", "instanceId", "process_start", "shutdown", "served"])
        per = Counter(o[0] for o in out)
        print("  instances per service: " + ", ".join(f"{k} {v}" for k, v in sorted(per.items()))
              + "  (1 = the same instance all day; more = replaced by the platform)")
        starts = Counter(e["resource"]["labels"].get("service_name", "") for e in syslog
                         if "Starting new instance" in (e.get("textPayload") or ""))
        print(f"  'Starting new instance' in the system log: {dict(sorted(starts.items()))}")

    # 5. cost -----------------------------------------------------------------------------------------
    if a.billing:
        print("\n5. Cost  [10.3 W4]")
        b = list(csv.DictReader(open(a.billing)))
        agg = defaultdict(lambda: defaultdict(float))
        for x in b:
            amt, g = float(x["usage_amount"] or 0), float(x["gross_cost_eur"] or 0)
            kind = ("min_cpu" if "Min Instance CPU" in x["sku"] else "min_mem" if "Min Instance Memory" in x["sku"]
                    else "req_cpu" if "CPU" in x["sku"] else "req_mem" if "Memory" in x["sku"]
                    else "requests" if x["sku"] == "Requests" else "other")
            agg[x["size"]][kind] += amt
            agg[x["size"]]["gross_" + ("min" if kind.startswith("min") else "req")] += g
            agg[x["size"]]["net"] += float(x["net_cost_eur"] or 0)
        out = []
        for s in sizes:
            d = agg[s]
            exist_h = d["min_cpu"] / 3600
            per_s = d["gross_min"] / d["min_cpu"] if d["min_cpu"] else 0
            out.append([s, round(exist_h, 2), round(d["min_mem"] / 2**30 / max(d["min_cpu"], 1), 2),
                        round(d["gross_min"], 4), round(d["gross_req"], 4), int(d["requests"]),
                        f"{per_s:.4e}", round(per_s * MONTH_S, 2)])
        table(out, ["size", "min_instance_h", "GiB_per_vCPU", "gross_min_eur", "gross_req_eur", "billed_req",
                    "eur_per_s", "month_30d_eur"])
        print("  min_instance_h = billed min-instance vCPU-seconds / 3600, i.e. how long the service existed")
        if a.zero_billing:
            z = [x for x in csv.DictReader(open(a.zero_billing))
                 if float(x["idle_min"]) == TIER and x["hour"].startswith("2026-11-19")]
            out = []
            for s in sizes:
                gz = sum(float(x["gross_cost_eur"] or 0) for x in z if x["size"] == s)
                d = agg[s]
                per_s = d["gross_min"] / d["min_cpu"] if d["min_cpu"] else 0
                zero_month = gz / 10 * (MONTH_S / 3600)    # 10 h window, same request pattern all month
                warm_month = per_s * MONTH_S
                out.append([s, round(gz, 4), round(zero_month, 2), round(warm_month, 2),
                            round(warm_month / zero_month, 1) if zero_month else ""])
            table(out, ["size", "zero_10h_eur", "zero_month_eur", "warm_month_eur", "warm_vs_zero_x"])
            print("  scale-to-zero month = its 10 h cost on 2026-11-19 x 72 (a request every 16 min, all month);")
            print("  always-warm month = min-instance price per second x 30 days (requests add little); gross")


if __name__ == "__main__":
    main()
