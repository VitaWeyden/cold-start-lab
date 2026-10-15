#!/usr/bin/env python3
"""Analysis of the boundary measurement (works on results3.csv and, with --legacy-drop-first,
on the Test 1 / Test 2 CSVs).

  python3 analyze3.py results3.csv
  python3 analyze3.py "Test 2/results_2.csv" --legacy-drop-first

Per idle tier: n, cold count, cold rate with 95% Wilson CI, real idle time actually waited.
Then the boundary: the bracket between the longest idle that stayed warm and the shortest
that was cold; if the tiers overlap (mixed outcomes) a logistic fit gives the 50% point with
a bootstrap CI. Finally the latency split of cold vs warm requests.

--legacy-drop-first: the first idle sample of Test 1/2 was not a real "X minutes idle" sample
(it followed a long gap after deploy / the previous test, so the instance was already gone);
the Test 3 warm-up request makes this unnecessary there.
"""
import argparse
import csv
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"),) * 2
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - h) / d) * 100, min(1.0, (c + h) / d) * 100


def load(path, drop_first):
    rows = list(csv.DictReader(open(path)))
    rows = [r for r in rows if r["variant"] == "scale-zero"]
    if drop_first:  # first row of the file = first idle sample after a long gap
        first = min((r for r in rows if r["scenario"].startswith("idle_")),
                    key=lambda r: datetime.fromisoformat(r["timestamp_utc"]))
        rows.remove(first)
    out, errs = [], defaultdict(int)
    for r in rows:
        if not r["scenario"].startswith("idle_"):
            continue
        nominal = float(r["scenario"][5:-3])
        if r["error"] or r["http_status"] != "200":
            errs[nominal] += 1  # attempted but unusable: shown in the table, not counted as cold or warm
            continue
        a = r.get("actual_idle_s", "")
        out.append({"tier": nominal, "idle": float(a) / 60 if a else nominal,
                    "cold": r["cold"] == "True", "row": r})
    return out, errs


def logistic(points):
    """Newton-Raphson logistic regression y ~ b0 + b1*x. Returns (b0, b1) or None."""
    b0, b1 = 0.0, 0.0
    for _ in range(60):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in points:
            p = 1 / (1 + math.exp(-max(-30, min(30, b0 + b1 * x))))
            w = p * (1 - p)
            g0 += y - p
            g1 += (y - p) * x
            h00 += w
            h01 += w * x
            h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if det < 1e-9:
            return None
        d0, d1 = (h11 * g0 - h01 * g1) / det, (-h01 * g0 + h00 * g1) / det
        b0, b1 = b0 + d0, b1 + d1
        if abs(d0) + abs(d1) < 1e-8:
            return (b0, b1) if b1 > 0 else None
    return None


def x50(points, centre):
    fit = logistic([(x - centre, y) for x, y in points])
    return None if fit is None else centre - fit[0] / fit[1]


def med(vals):
    vals = [float(v) for v in vals if v not in ("", None)]
    return round(statistics.median(vals), 1) if vals else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--legacy-drop-first", action="store_true")
    ap.add_argument("--out", default="summary3.csv")
    a = ap.parse_args()
    data, errs = load(a.csv, a.legacy_drop_first)
    by = defaultdict(list)
    for d in data:
        by[d["tier"]].append(d)

    hdr = ["tier_min", "n", "errors", "cold", "cold_rate_pct", "ci95_lo", "ci95_hi",
           "mean_actual_idle_min", "median_ms", "p95_ms"]
    rows = []
    for tier in sorted(by):
        v = by[tier]
        k, n = sum(x["cold"] for x in v), len(v)
        lo, hi = wilson(k, n)
        lat = sorted(float(x["row"]["latency_ms"]) for x in v)
        p95 = lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]
        rows.append([f"{tier:g}", n, errs.get(tier, 0), k, round(100 * k / n, 1), round(lo, 1), round(hi, 1),
                     round(statistics.mean(x["idle"] for x in v), 2), round(statistics.median(lat), 1), round(p95, 1)])
    for tier in sorted(set(errs) - set(by)):  # tiers where every attempt failed
        rows.append([f"{tier:g}", 0, errs[tier], 0, "", "", "", "", "", ""])
    rows.sort(key=lambda r: float(r[0]))
    print("  ".join(f"{h:>20}" for h in hdr))
    for r in rows:
        print("  ".join(f"{c!s:>20}" for c in r))
    with open(a.out, "w", newline="") as f:
        csv.writer(f).writerows([hdr] + rows)

    warm = [d["idle"] for d in data if not d["cold"]]
    cold = [d["idle"] for d in data if d["cold"]]
    print("\nBoundary")
    if not warm or not cold:
        print("  only one outcome observed - the boundary is outside the measured range")
    else:
        tiers = sorted(by)
        rate = {t: sum(x["cold"] for x in by[t]) / len(by[t]) for t in tiers}
        first = next((t for t in tiers if rate[t] >= 0.5), None)
        if first is None:
            print("  cold rate never reaches 50% in the measured range")
        elif first == tiers[0]:
            print(f"  cold rate is already >= 50% at the shortest tier ({first:g} min)")
        else:
            prev = tiers[tiers.index(first) - 1]
            kp, kf = sum(x["cold"] for x in by[prev]), sum(x["cold"] for x in by[first])
            print(f"  cold rate crosses 50% between {prev:g} min ({kp}/{len(by[prev])} cold) "
                  f"and {first:g} min ({kf}/{len(by[first])} cold)")
        print(f"  warm samples up to {max(warm):.2f} min idle, cold samples from {min(cold):.2f} min idle")
        mixed = [t for t in tiers if 0 < sum(x["cold"] for x in by[t]) < len(by[t])]
        if len(mixed) >= 2:  # a logistic fit needs mixed outcomes at two or more idle times
            pts = [(d["idle"], 1.0 if d["cold"] else 0.0) for d in data]
            centre = statistics.mean(x for x, _ in pts)
            est = x50(pts, centre)
            if est is None:
                print("  logistic fit did not converge")
            else:
                random.seed(1)
                bs = [e for e in (x50([random.choice(pts) for _ in pts], centre) for _ in range(2000)) if e is not None]
                bs.sort()
                ci = (bs[int(0.025 * len(bs))], bs[int(0.975 * len(bs)) - 1]) if len(bs) > 50 else None
                print(f"  logistic fit: 50% cold at {est:.2f} min idle" +
                      (f"  (95% bootstrap CI {ci[0]:.2f}-{ci[1]:.2f}, {len(bs)}/2000 valid resamples)" if ci else ""))
        else:
            print("  at most one tier has mixed outcomes: the transition is a sharp step and a logistic fit has no")
            print("  finite solution; use the crossing above, the per-tier CIs and teardown_times.py instead")

    print("\nLatency split, median ms (cold vs warm requests)")
    parts = ["latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "body_ms", "startup_ms", "init_ms", "work_ms"]
    print(f"{'':>6}" + "".join(f"{p:>13}" for p in parts))
    for label, flag in (("cold", True), ("warm", False)):
        sel = [d["row"] for d in data if d["cold"] == flag]
        if sel:
            print(f"{label:>6}" + "".join(f"{med([r.get(p, '') for r in sel]):>13}" for p in parts) + f"   (n={len(sel)})")
    print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()
