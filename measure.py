#!/usr/bin/env python3
"""Latency measurement for the two Cloud Run variants (standard library only).

Two scenarios:
  idle_<X>min   : one request to BOTH services (in parallel) after X minutes
                  of idleness, repeated --repeats times
  sequential    : back-to-back requests (the "normal", warm latency)

Results are appended to a CSV, flushed row by row (nothing is lost if the run is interrupted).

Examples:
  python3 measure.py --urls-file urls.env                       # full measurement (/api/report)
  python3 measure.py --urls-file urls.env --path / --out results_light.csv   # light endpoint
  python3 measure.py --urls-file urls.env --idle 5 20 --repeats 10
  python3 measure.py --urls-file urls.env --idle 0.05 --repeats 2 --sequential 10   # quick trial
"""
import argparse
import csv
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

FIELDS = [
    "timestamp_utc", "scenario", "variant", "iteration", "latency_ms",
    "http_status", "cold", "instance_id", "instance_uptime_s", "request_count", "error",
]


def load_urls(path):
    urls = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                urls[k] = v
    return urls["SCALE_ZERO_URL"], urls["WARM_URL"]


def timed_get(url, timeout=60):
    """One GET request; latency is the full request-response time seen by the client."""
    row = {"latency_ms": "", "http_status": "", "cold": "", "instance_id": "",
           "instance_uptime_s": "", "request_count": "", "error": ""}
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            row["http_status"] = resp.status
        info = json.loads(body)
        row["cold"] = info.get("cold", "")
        row["instance_id"] = info.get("instance_id", "")
        row["instance_uptime_s"] = info.get("instance_uptime_s", "")
        row["request_count"] = info.get("request_count", "")
    except Exception as e:  # noqa: BLE001
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        row["error"] = repr(e)
    return row


class Writer:
    def __init__(self, path):
        new = True
        try:
            new = open(path).read(1) == ""
        except FileNotFoundError:
            pass
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=FIELDS)
        if new:
            self.w.writeheader()
        self.lock = threading.Lock()

    def write(self, scenario, variant, iteration, row):
        row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "scenario": scenario, "variant": variant, "iteration": iteration, **row}
        with self.lock:
            self.w.writerow(row)
            self.f.flush()
        print(f"  {scenario:>12} {variant:<11} #{iteration:<3} "
              f"{row['latency_ms']:>9} ms  cold={row['cold']!s:<5} "
              f"inst={row['instance_id']} {row['error']}", flush=True)


def paired_request(writer, scenario, iteration, targets):
    """Both services get the request at the same time, so conditions are as equal as possible."""
    def run(variant, url):
        writer.write(scenario, variant, iteration, timed_get(url))
    threads = [threading.Thread(target=run, args=(v, u)) for v, u in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def sleep_with_countdown(minutes):
    end = time.time() + minutes * 60
    while True:
        left = end - time.time()
        if left <= 0:
            break
        print(f"\r  waiting: {int(left // 60):02d}:{int(left % 60):02d} left ", end="", flush=True)
        time.sleep(min(1, left))
    print("\r" + " " * 40 + "\r", end="")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--urls-file", default="urls.env")
    ap.add_argument("--scale-zero-url")
    ap.add_argument("--warm-url")
    ap.add_argument("--path", default="/api/report", help="request path, e.g. / (light), /api/products, /api/report (heavy)")
    ap.add_argument("--idle", type=float, nargs="*", default=[5, 20],
                    help="idle times in minutes (decimals allowed), default: 5 20")
    ap.add_argument("--repeats", type=int, default=10, help="repetitions per idle time (>=10)")
    ap.add_argument("--sequential", type=int, default=100, help="number of back-to-back requests (0 = skip)")
    ap.add_argument("--gap-ms", type=int, default=200, help="pause between back-to-back requests")
    ap.add_argument("--out", default="results.csv")
    args = ap.parse_args()

    if args.scale_zero_url and args.warm_url:
        sz, warm = args.scale_zero_url, args.warm_url
    else:
        sz, warm = load_urls(args.urls_file)
    targets = [("scale-zero", sz.rstrip("/") + args.path), ("always-warm", warm.rstrip("/") + args.path)]
    writer = Writer(args.out)

    # 1) first request after an idle period
    for idle in args.idle:
        label = f"idle_{idle:g}min"
        print(f"\n== {label}: {args.repeats} repetitions ==")
        for i in range(1, args.repeats + 1):
            print(f"[{label}] {i}/{args.repeats}")
            sleep_with_countdown(idle)
            paired_request(writer, label, i, targets)

    # 2) back-to-back requests (both instances are warm by now)
    if args.sequential > 0:
        print(f"\n== sequential: {args.sequential} requests ==")
        paired_request(writer, "warmup", 0, targets)  # warm-up, recorded as its own scenario
        for i in range(1, args.sequential + 1):
            paired_request(writer, "sequential", i, targets)
            time.sleep(args.gap_ms / 1000)

    print(f"\nDone -> {args.out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted (rows written so far are kept in the CSV).")
