#!/usr/bin/env python3
"""Presentation charts from the Test 1-3 results.

Writes three PNGs into ./plots (or --out):
  fig1_latency.png    response time of every cold and warm request
  fig2_cold_rate.png  cold-start rate per idle time, Tests 1-3, 95% Wilson CI
  fig3_teardown.png   when Cloud Run sent SIGTERM after the last request (Test 3)

Every scale-zero restart is counted, including the first request of a run.
Only HTTP 200 rows are used; warm-up requests and the back-to-back
"sequential" series are left out.

  pip install matplotlib
  python3 plots.py                 # run from the project folder
  python3 plots.py --out slides    # another output folder
"""
import argparse
import csv
import math
import os
import random
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLD = "#2257B8"      # blue  = cold start / scale-zero
WARM = "#C8631F"      # orange = warm instance
INK = "#14202B"
MUTED = "#5F6A74"
GRID = "#E2DED5"
BG = "#FFFFFF"

TESTS = {
    "Test 1": "Test 1/results_1.csv",
    "Test 2": "Test 2/results_2.csv",
    "Test 3": "Test 3/results3.csv",
}
TEARDOWN = "Test 3/teardown3.csv"


def style(ax):
    ax.set_facecolor(BG)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=12)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def idle_rows(path):
    """HTTP 200 idle-tier rows of a results file: (idle_min, variant, cold, latency_ms)."""
    out = []
    for r in read_rows(path):
        sc = r["scenario"]
        if not sc.startswith("idle_") or r.get("http_status", "200") != "200":
            continue
        idle = float(sc[len("idle_"):-len("min")])
        out.append((idle, r["variant"], r["cold"] == "True", float(r["latency_ms"])))
    return out


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def fig_latency(data, out):
    cold = [lat for rows in data.values() for (_, _, c, lat) in rows if c]
    warm = [lat for rows in data.values() for (_, _, c, lat) in rows if not c]
    rnd = random.Random(1)
    fig, ax = plt.subplots(figsize=(10, 6), dpi=200)
    fig.patch.set_facecolor(BG)
    style(ax)
    for x, vals, color in ((0, warm, WARM), (1, cold, COLD)):
        xs = [x + rnd.uniform(-0.18, 0.18) for _ in vals]
        ax.scatter(xs, vals, s=46, color=color, alpha=0.75, edgecolors=BG, linewidths=1)
        med = statistics.median(vals)
        ax.hlines(med, x - 0.3, x + 0.3, color=INK, linewidth=2)
        ax.text(x + 0.33, med if x else med + 120, f"median {med:,.0f} ms\nn = {len(vals)}",
                va="center" if x else "bottom", fontsize=13, color=INK)
    ax.set_ylim(0, 8000)
    ax.set_yticks(range(0, 8001, 1000))
    ax.set_yticklabels([f"{s} s" for s in range(0, 9)])
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["Warm request", "Cold request"], fontsize=14, color=INK)
    ax.set_xlim(-0.6, 1.9)
    ax.set_ylabel("End-to-end response time", fontsize=13, color=MUTED)
    ax.set_title("Response time after an idle period, Tests 1-3", loc="left",
                 fontsize=16, color=INK, pad=14)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig1_latency.png"), facecolor=BG)
    plt.close(fig)


def fig_cold_rate(data, out):
    # Points only, no connecting lines: between two tested idle times nothing was measured.
    # Each test is nudged a little sideways so overlapping points stay visible.
    # Each test has its own colour AND marker, so the tests stay apart for colour-blind
    # readers too (palette checked with the dataviz validator, CVD-safe with markers).
    markers = {"Test 1": "o", "Test 2": "s", "Test 3": "^"}
    colors = {"Test 1": "#2257B8", "Test 2": "#12936A", "Test 3": "#D4499A"}
    nudge = {"Test 1": 0.97, "Test 2": 1.0, "Test 3": 1.03}
    fig, ax = plt.subplots(figsize=(10, 6), dpi=200)
    fig.patch.set_facecolor(BG)
    style(ax)
    ax.axvspan(15, 16, color="#ECE8E0", zorder=0)
    ax.axvline(15, color=MUTED, linestyle="--", linewidth=1.5)
    for name, rows in data.items():
        tiers = {}
        for idle, variant, cold, _ in rows:
            if variant != "scale-zero":
                continue
            k, n = tiers.get(idle, (0, 0))
            tiers[idle] = (k + cold, n + 1)
        xs = sorted(tiers)
        ps = [100 * tiers[x][0] / tiers[x][1] for x in xs]
        lo = [p - 100 * wilson(*tiers[x])[0] for p, x in zip(ps, xs)]
        hi = [100 * wilson(*tiers[x])[1] - p for p, x in zip(ps, xs)]
        ax.errorbar([x * nudge[name] for x in xs], ps, yerr=[lo, hi], color=colors[name],
                    marker=markers[name], markersize=10, linestyle="none", capsize=4,
                    elinewidth=1.2, markeredgecolor=BG, label=name)
    ax.set_xscale("log")
    ticks = [5, 10, 15, 20, 30, 60]
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks])
    ax.minorticks_off()
    ax.set_ylim(-5, 105)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("Idle time before the request (minutes)", fontsize=13, color=MUTED)
    ax.set_ylabel("Cold starts, scale-zero (95% CI)", fontsize=13, color=MUTED)
    ax.set_title("Cold-start rate jumps between 15 and 16 minutes", loc="left",
                 fontsize=16, color=INK, pad=14)
    ax.legend(frameon=False, fontsize=12, loc="center right", labelcolor=INK)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig2_cold_rate.png"), facecolor=BG)
    plt.close(fig)


def fig_teardown(path, out):
    vals = [float(r["teardown_after_s"]) for r in read_rows(path) if r["teardown_after_s"]]
    main = [v for v in vals if v > 600]          # the 166 s replacement is not an idle teardown
    outliers = len(vals) - len(main)
    fig, ax = plt.subplots(figsize=(10, 6), dpi=200)
    fig.patch.set_facecolor(BG)
    style(ax)
    bins = list(range(886, 914, 2))
    ax.hist(main, bins=bins, color=COLD, edgecolor=BG, linewidth=2)
    ax.set_xticks(bins)          # one tick per bar edge, so every bar sits between two labels
    ax.set_xlim(bins[0], bins[-1])
    ax.axvline(900, color=MUTED, linestyle="--", linewidth=1.5)
    ax.text(899.6, ax.get_ylim()[1] * 0.95, "900 s = 15:00", fontsize=12, color=MUTED,
            va="top", ha="right")
    med = statistics.median(main)
    ax.set_xlabel("Seconds from the last response to SIGTERM", fontsize=13, color=MUTED)
    ax.set_ylabel("Cold samples", fontsize=13, color=MUTED)
    note = f"median {med:.1f} s, range {min(main):.1f}-{max(main):.1f} s, n = {len(main)}"
    if outliers:
        note += f" ({outliers} early replacement left out)"
    ax.set_title("When Cloud Run removes an idle instance (Test 3)", loc="left",
                 fontsize=16, color=INK, pad=28)
    ax.text(0, 1.02, note, transform=ax.transAxes, fontsize=12, color=MUTED)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig3_teardown.png"), facecolor=BG)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="plots", help="output folder (default: plots)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    data = {name: idle_rows(path) for name, path in TESTS.items()}
    fig_latency(data, args.out)
    fig_cold_rate(data, args.out)
    fig_teardown(TEARDOWN, args.out)
    print(f"Wrote fig1_latency.png, fig2_cold_rate.png, fig3_teardown.png to {args.out}/")


if __name__ == "__main__":
    main()