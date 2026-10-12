#!/usr/bin/env python3
"""For every cold sample in results3.csv, find when Cloud Run actually shut the previous
instance down (the uvicorn "Shutting down" line written on SIGTERM) and print how long
after the last request that happened. Cold samples only; warm ones have no shutdown.

  gcloud logging read 'resource.type="cloud_run_revision" AND
      resource.labels.service_name=~"^coldstart-idle-" AND textPayload:"Shutting down"' \
      --project "$PROJECT_ID" --freshness=2d --format=json > shutdown_logs.json
  python3 teardown_times.py results3.csv shutdown_logs.json > teardown3.csv

Pilot first: check that shutdown_logs.json really contains entries for a service that was
torn down. If the log text differs, change NEEDLE / look at the raw entries.
"""
import csv
import json
import sys
from datetime import datetime

NEEDLE = "Shutting down"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(results, logs):
    entries = {}
    for e in json.load(open(logs)):
        text = e.get("textPayload", "") or ""
        if NEEDLE in text:
            svc = e["resource"]["labels"]["service_name"]
            entries.setdefault(svc, []).append(ts(e["timestamp"]))
    w = csv.writer(sys.stdout)
    w.writerow(["service", "iteration", "idle_min_tier", "actual_idle_s", "teardown_after_s"])
    for r in csv.DictReader(open(results)):
        if r["cold"] != "True" or not r["prev_end_utc"]:
            continue
        lo, hi = ts(r["prev_end_utc"]), ts(r["request_start_utc"])
        hits = [t for t in entries.get(r["service"], []) if lo <= t <= hi]
        w.writerow([r["service"], r["iteration"], r["idle_min"], r["actual_idle_s"],
                    round((min(hits) - lo).total_seconds(), 1) if hits else ""])


if __name__ == "__main__":
    main(*sys.argv[1:3])
