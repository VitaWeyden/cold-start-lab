#!/usr/bin/env python3
"""Test 4 measurement (used first for the max-instances pilot, see "Test 4/Test4_report.md").

Same idea as measure3.py (one thread per service, a warm-up request, then REPS samples, each
exactly IDLE minutes after the END of the previous response), with the fixes that Test 3 needed:

  * Billing export gate. Before anything is sent, `bq ls` must show a
    gcp_billing_export_v1_* table in the billing dataset; otherwise the script stops.
    (Test 3: the export was never switched on and is not backfilled, so its cost was lost.)
  * Request id on every attempt. A 32-hex id is sent as X-Request-Id (echoed by the app) and as
    the trace id in X-Cloud-Trace-Context / traceparent, so every client row can be joined with
    the Cloud Run request log -- including 429s, which never reach the application.
  * Immediate retry after a failed attempt (HTTP 429, 5xx or a connection error), up to
    --max-retries, every attempt as its own row (attempt = 1, 2, ...). `time_to_success_ms` is
    the time from the start of attempt 1 to the end of the successful attempt, i.e. what a user
    would have waited. The next idle period starts at the end of the LAST attempt.
  * Staggered starts. Each service gets its own offset (services file column offset_s), so cold
    starts of different services do not fall in the same second (Test 3: all at once).
  * Fixed time window. --start-at waits until a given UTC time, so the run happens in the window
    written in the plan; the actual start / end is written to the meta file.

Services file (written by deploy4p.sh):  arm,replica,idle_min,offset_s,name,url
Run it from the "Test 4" folder: all inputs and outputs are relative to the current directory.
Standard library only. Rows are appended and flushed one by one.

  python3 measure4.py --services services4p.csv --dry-run
  python3 measure4.py --services services4p.csv --reps 8 --start-at 2026-10-30T14:00Z \\
      --out results4p.csv --meta run4p_meta.txt
"""
import argparse
import csv
import http.client
import json
import os
import platform
import random
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

FIELDS = [
    "timestamp_utc", "scenario", "arm", "replica", "service", "idle_min", "iteration", "attempt",
    "request_id", "first_request_id", "prev_end_utc", "request_start_utc", "actual_idle_s",
    "latency_ms", "time_to_success_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "body_ms",
    "http_status", "cold", "instance_id", "cloud_run_instance_id", "instance_uptime_s",
    "request_count", "startup_ms", "init_ms", "work_ms", "revision", "echo_request_id", "error",
]
SERVER_FIELDS = ["cold", "instance_id", "cloud_run_instance_id", "instance_uptime_s", "request_count",
                 "startup_ms", "init_ms", "work_ms", "revision"]
RETRY_STATUSES = {429, 500, 502, 503, 504}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_utc(s):
    s = s.strip().replace("Z", "+00:00")
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- billing export gate
def billing_gate(project, dataset):
    """Returns (ok, message). ok only if a gcp_billing_export_v1_* table exists."""
    if not shutil.which("bq"):
        return False, "the `bq` CLI is not installed / not on PATH"
    ref = f"{project}:{dataset}"
    p = subprocess.run(["bq", "ls", "--format=json", "--max_results=1000", ref],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return False, f"`bq ls {ref}` failed: {(p.stderr or p.stdout).strip()[:400]}"
    try:
        tables = [t["tableReference"]["tableId"] for t in json.loads(p.stdout or "[]")]
    except (ValueError, KeyError):
        return False, f"could not parse `bq ls` output: {p.stdout[:200]}"
    std = [t for t in tables if t.startswith("gcp_billing_export_v1_")]
    if not std:
        return False, (f"no gcp_billing_export_v1_* table in {ref} (found: {tables or 'nothing'}). "
                       "Switch on Billing -> Billing export -> BigQuery export -> Standard usage cost "
                       "and wait until the table appears.")
    msg = f"billing export table found: {ref}.{std[0]}"
    # Freshness is informative only: the table exists = the export is on.
    q = (f"SELECT FORMAT_TIMESTAMP('%FT%TZ', MAX(export_time)) AS last_export "
         f"FROM `{project}.{dataset}.{std[0]}`")
    p = subprocess.run(["bq", "query", "--nouse_legacy_sql", "--format=json", "--quiet", q],
                       capture_output=True, text=True)
    if p.returncode == 0:
        try:
            msg += f"; last export_time {json.loads(p.stdout)[0]['last_export']}"
        except (ValueError, KeyError, IndexError):
            pass
    return True, msg


# ---------------------------------------------------------------- one timed request
def new_request_id():
    return uuid.uuid4().hex  # 32 hex = a valid trace id


def timed_get(url, rid, timeout=60):
    """One GET on a fresh connection, IPv4 only, split into phases (ms)."""
    row = {k: "" for k in ("latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "body_ms",
                           "http_status", "error", "echo_request_id", *SERVER_FIELDS)}
    u = urlsplit(url)
    https = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if https else 80)
    target = (u.path or "/") + (("?" + u.query) if u.query else "")
    span = random.getrandbits(63) or 1
    headers = {"Connection": "close", "User-Agent": "coldstart-measure/4",
               "X-Request-Id": rid,
               "X-Cloud-Trace-Context": f"{rid}/{span};o=1",
               "traceparent": f"00-{rid}-{span:016x}-01"}
    t0 = time.perf_counter()
    conn = None
    try:
        ai = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0]
        t1 = time.perf_counter()
        sock = socket.socket(ai[0], ai[1])
        sock.settimeout(timeout)
        sock.connect(ai[4])
        t2 = time.perf_counter()
        if https:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        t3 = time.perf_counter()
        conn = (http.client.HTTPSConnection if https else http.client.HTTPConnection)(
            host, port, timeout=timeout)
        conn.sock = sock
        conn.request("GET", target, headers=headers)
        t4 = time.perf_counter()
        resp = conn.getresponse()
        t5 = time.perf_counter()
        body = resp.read()
        t6 = time.perf_counter()
        row.update(
            latency_ms=round((t6 - t0) * 1000, 2), dns_ms=round((t1 - t0) * 1000, 2),
            tcp_ms=round((t2 - t1) * 1000, 2), tls_ms=round((t3 - t2) * 1000, 2),
            ttfb_ms=round((t5 - t4) * 1000, 2), body_ms=round((t6 - t5) * 1000, 2),
            http_status=resp.status)
        try:
            info = json.loads(body)
            for k in SERVER_FIELDS:
                row[k] = info.get(k, "")
            row["echo_request_id"] = info.get("request_id", "")
        except ValueError:
            row["error"] = "non-JSON body: " + body[:80].decode("utf-8", "replace").replace("\n", " ")
    except Exception as e:  # noqa: BLE001
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        row["error"] = repr(e)
    finally:
        if conn is not None:
            conn.close()
    return row


def failed(row):
    return row["http_status"] == "" or int(row["http_status"]) in RETRY_STATUSES


# ---------------------------------------------------------------- output
class Writer:
    def __init__(self, path):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=FIELDS)
        if new:
            self.w.writeheader()
        self.lock = threading.Lock()
        self.count = 0

    def write(self, row):
        row = {"timestamp_utc": now_utc(), **row}
        with self.lock:
            self.w.writerow(row)
            self.f.flush()
            self.count += 1
            idle = row.get("actual_idle_s", "")
            print(f"{row['timestamp_utc'][11:19]} {row['service']:<20} {row['scenario']:<12} "
                  f"#{row['iteration']:<3}a{row['attempt']} "
                  f"idle={'' if idle == '' else format(idle, '.1f') + 's':<8} "
                  f"{row['http_status']!s:>3} {row['latency_ms']:>9} ms cold={row['cold']!s:<5} "
                  f"inst={row['instance_id']} {row['error'][:60]}", flush=True)


def sleep_until(mono_target, stop):
    while not stop.is_set():
        left = mono_target - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


def attempt_series(writer, url, common, scenario, iteration, extra, max_retries, retry_delay):
    """Attempt 1 plus immediate retries. Returns the monotonic end time of the last attempt."""
    first_rid = new_request_id()
    first_start = time.monotonic()
    for attempt in range(1, max_retries + 2):
        rid = first_rid if attempt == 1 else new_request_id()
        start_utc = now_utc()
        r = timed_get(url, rid)
        end = time.monotonic()
        ok = not failed(r)
        writer.write({**common, "scenario": scenario, "iteration": iteration, "attempt": attempt,
                      "request_id": rid, "first_request_id": first_rid,
                      "request_start_utc": start_utc,
                      "time_to_success_ms": round((end - first_start) * 1000, 2) if ok else "",
                      **(extra if attempt == 1 else {}), **r})
        if ok:
            break
        if attempt <= max_retries and retry_delay > 0:
            time.sleep(retry_delay)
    return end


def run_service(writer, svc, args, t_zero, stop):
    url = svc["url"].rstrip("/") + args.path
    idle_min = float(svc["idle_min"])
    common = {"arm": svc["arm"], "replica": svc["replica"], "service": svc["name"], "idle_min": idle_min}
    sleep_until(t_zero + float(svc["offset_s"]), stop)
    if stop.is_set():
        return
    end = None
    for w in range(1, args.warmups + 1):
        end = attempt_series(writer, url, common, "warmup", w, {}, args.max_retries, args.retry_delay)
    prev_end_mono, prev_end_utc = end, now_utc()
    scen = f"idle_{idle_min:g}min"
    for i in range(1, args.reps + 1):
        sleep_until(prev_end_mono + idle_min * 60, stop)
        if stop.is_set():
            return
        extra = {"prev_end_utc": prev_end_utc,
                 "actual_idle_s": round(time.monotonic() - prev_end_mono, 2)}
        prev_end_mono = attempt_series(writer, url, common, scen, i, extra, args.max_retries, args.retry_delay)
        prev_end_utc = now_utc()


def load_services(path):
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r.get("name")]
    need = {"arm", "replica", "idle_min", "offset_s", "name", "url"}
    if not rows or not need <= set(rows[0]):
        raise SystemExit(f"{path}: expected header {','.join(sorted(need))}")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--services", default="services4p.csv")
    ap.add_argument("--reps", type=int, default=8, help="measured samples per service")
    ap.add_argument("--path", default="/api/report")
    ap.add_argument("--warmups", type=int, default=1)
    ap.add_argument("--max-retries", type=int, default=5, help="retries after a failed attempt")
    ap.add_argument("--retry-delay", type=float, default=0.0, help="seconds between retries (0 = immediate)")
    ap.add_argument("--start-at", help="UTC start time, e.g. 2026-10-30T14:00Z (default: now)")
    ap.add_argument("--project", default=os.environ.get("PROJECT_ID", ""))
    ap.add_argument("--billing-dataset", default="billing_export")
    ap.add_argument("--skip-billing-check", action="store_true",
                    help="ONLY for local trials: start without the billing export (written to the meta file)")
    ap.add_argument("--out", default="results4p.csv")
    ap.add_argument("--meta", default="run4p_meta.txt")
    ap.add_argument("--dry-run", action="store_true", help="check the gate, print the plan and exit")
    args = ap.parse_args()

    services = load_services(args.services)
    start = parse_utc(args.start_at) if args.start_at else datetime.now(timezone.utc)
    longest = max(float(s["offset_s"]) + float(s["idle_min"]) * 60 * args.reps for s in services)
    h, rem = divmod(int(longest), 3600)
    print(f"Plan: {len(services)} services in parallel, {args.reps} samples each, path {args.path}")
    for s in services:
        print(f"  {s['arm']:<6} {s['replica']:<3} idle {float(s['idle_min']):g} min  "
              f"offset {float(s['offset_s']):>5g} s  {s['name']}  {s['url']}")
    print(f"Window (UTC): {start:%Y-%m-%d %H:%M} -> ~{datetime.fromtimestamp(start.timestamp() + longest, timezone.utc):%H:%M}"
          f"  (~{h}h {rem // 60:02d}m plus request time)")

    if args.skip_billing_check:
        gate_ok, gate_msg = True, "SKIPPED (--skip-billing-check)"
        print("!! Billing export check SKIPPED - do not use this run for cost figures.")
    else:
        if not args.project:
            raise SystemExit("Set --project or PROJECT_ID (needed for the billing export check).")
        gate_ok, gate_msg = billing_gate(args.project, args.billing_dataset)
        print(("OK  " if gate_ok else "STOP ") + gate_msg)
    if not gate_ok:
        raise SystemExit("Billing export is not ready - not starting. (Fix it, or for a local trial only: "
                         "--skip-billing-check)")
    if args.dry_run:
        return

    with open(args.meta, "a") as m:
        m.write(f"planned_start_utc={start.isoformat()}\nbilling_check={gate_msg}\n"
                f"args={vars(args)}\nclient={platform.node()} python={platform.python_version()}\n")
    wait = start.timestamp() - time.time()
    if wait > 0:
        print(f"Waiting {wait / 60:.1f} min until {start:%H:%M:%S} UTC ...", flush=True)
    stop = threading.Event()
    try:
        while time.time() < start.timestamp():
            time.sleep(min(1.0, start.timestamp() - time.time()))
    except KeyboardInterrupt:
        sys.exit("\nInterrupted before the start.")
    actual_start = now_utc()
    t_zero = time.monotonic()
    writer = Writer(args.out)
    threads = [threading.Thread(target=run_service, args=(writer, s, args, t_zero, stop)) for s in services]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        with open(args.meta, "a") as m:
            m.write(f"actual_start_utc={actual_start}\ninterrupted_utc={now_utc()}\nrows={writer.count}\n")
        sys.exit("\nInterrupted (rows written so far are kept in the CSV).")
    with open(args.meta, "a") as m:
        m.write(f"actual_start_utc={actual_start}\nend_utc={now_utc()}\nrows={writer.count}\n")
    print(f"\nDone -> {args.out}  (start {actual_start}, end {now_utc()})")


if __name__ == "__main__":
    main()