#!/usr/bin/env python3
"""Summarizes results.csv: count, cold start count, median, p95, mean, min, max.

Usage:
  python3 analyze.py results.csv     # table + summary.csv (+ latency.png if matplotlib is installed)
"""
import csv
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
        rows.append({
            "scenario": scenario, "variant": variant, "n": len(vals),
            "cold_starts": cold[(scenario, variant)], "errors": errors[(scenario, variant)],
            "median_ms": round(statistics.median(vals), 1),
            "p95_ms": round(percentile(vals, 95), 1),
            "mean_ms": round(statistics.mean(vals), 1),
            "min_ms": round(min(vals), 1), "max_ms": round(max(vals), 1),
        })

    hdr = ["scenario", "variant", "n", "cold_starts", "errors", "median_ms", "p95_ms", "mean_ms", "min_ms", "max_ms"]
    print("  ".join(f"{h:>12}" for h in hdr))
    for r in rows:
        print("  ".join(f"{r[h]!s:>12}" for h in hdr))

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
