#!/usr/bin/env python3
"""Summarizes results.csv: count, cold start count, median, p95, mean, min, max.

Usage:
  python3 analyze.py results.csv     # table + summary.csv (+ latency.png if matplotlib is installed)
"""
import csv
import math
import statistics
import sys
from collections import defaultdict


def percentile(values, p):
    """Linear-interpolation percentile (same as numpy's default)."""
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def wilson_ci(successes, trials, z=1.96):
    """95% Wilson score interval for a cold-start rate - more honest than a
    raw percentage at small n (e.g. a "mixed" idle tier near the threshold),
    since it doesn't collapse to 0 width at 0% or 100% the way a naive
    +/- sqrt(p(1-p)/n) interval does."""
    if trials == 0:
        return (float("nan"), float("nan"))
    p = successes / trials
    denom = 1 + z**2 / trials
    centre = p + z**2 / (2 * trials)
    half = z * math.sqrt(p * (1 - p) / trials + z**2 / (4 * trials**2))
    return (max(0.0, (centre - half) / denom) * 100, min(1.0, (centre + half) / denom) * 100)


def main(path):
    groups = defaultdict(list)
    cold = defaultdict(int)
    errors = defaultdict(int)
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["scenario"] == "warmup":
                continue
            key = (r["scenario"], r["variant"])
            if r["error"] or r["http_status"] != "200":
                errors[key] += 1
                continue
            groups[key].append(float(r["latency_ms"]))
            cold[key] += r["cold"] == "True"

    rows = []
    for (scenario, variant), vals in sorted(groups.items()):
        n = len(vals)
        colds = cold[(scenario, variant)]
        errs = errors[(scenario, variant)]
        trials = n + errs  # every attempted repetition, including failed requests
        ci_lo, ci_hi = wilson_ci(colds, trials)
        rows.append({
            "scenario": scenario, "variant": variant, "n": n,
            "cold_starts": colds, "errors": errs,
            "cold_rate_pct": round(100 * colds / trials, 1) if trials else float("nan"),
            "cold_rate_ci95_lo": round(ci_lo, 1), "cold_rate_ci95_hi": round(ci_hi, 1),
            "median_ms": round(statistics.median(vals), 1),
            "p95_ms": round(percentile(vals, 95), 1),
            "mean_ms": round(statistics.mean(vals), 1),
            "min_ms": round(min(vals), 1), "max_ms": round(max(vals), 1),
        })

    hdr = ["scenario", "variant", "n", "cold_starts", "errors",
           "cold_rate_pct", "cold_rate_ci95_lo", "cold_rate_ci95_hi",
           "median_ms", "p95_ms", "mean_ms", "min_ms", "max_ms"]
    widths = {h: max(12, len(h) + 1) for h in hdr}
    print("  ".join(f"{h:>{widths[h]}}" for h in hdr))
    for r in rows:
        print("  ".join(f"{r[h]!s:>{widths[h]}}" for h in hdr))

    with open("summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    print("\n-> summary.csv")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib is not installed, plot skipped: pip install matplotlib)")
        return

    scenarios = sorted({k[0] for k in groups})
    fig, axes = plt.subplots(1, len(scenarios), figsize=(4.2 * len(scenarios), 4.5), squeeze=False)
    for ax, sc in zip(axes[0], scenarios):
        variants = sorted(v for (s, v) in groups if s == sc)
        ax.boxplot([groups[(sc, v)] for v in variants], showfliers=True)
        ax.set_xticks(range(1, len(variants) + 1), variants)
        ax.set_title(sc)
        ax.set_ylabel("response time (ms)")
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig("latency.png", dpi=150)
    print("-> latency.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results.csv")