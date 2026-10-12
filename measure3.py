#!/usr/bin/env python3
"""Test 3: boundary measurement, one Cloud Run service per idle tier, all run in parallel.

Each line of the services file is `idle_min,name,url`. One thread per service:
  1. a warm-up request (recorded as scenario "warmup", never analysed), so every
     measured sample is a true "idle X minutes after a known previous request";
  2. REPS times: wait until exactly X minutes after the END of the previous response,
     send one request, record it.

Per sample it records the real idle time (actual_idle_s, monotonic clock), the
client-side breakdown (DNS / TCP / TLS / time-to-first-byte / body), and the
server-reported fields (cold, instance_id, instance_uptime_s, startup_ms, ...).
Standard library only. Rows are flushed one by one and appended to the CSV.

  python3 measure3.py --services services3.csv --reps 20 --out results3.csv
"""
import argparse
import csv
import http.client
import json
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

FIELDS = [
    "timestamp_utc", "scenario", "variant", "service", "idle_min", "iteration",
    "prev_end_utc", "request_start_utc", "actual_idle_s",
    "latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "body_ms",
    "http_status", "cold", "instance_id", "instance_uptime_s", "request_count",
    "startup_ms", "init_ms", "work_ms", "revision", "error",
]
SERVER_FIELDS = ["cold", "instance_id", "instance_uptime_s", "request_count",
                 "startup_ms", "init_ms", "work_ms", "revision"]


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def timed_get(url, timeout=60):
    """One GET on a fresh connection, with the time split into its phases (ms)."""
    row = {k: "" for k in ("latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms",
                           "body_ms", "http_status", "error", *SERVER_FIELDS)}
    u = urlsplit(url)
    https = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if https else 80)
    target = (u.path or "/") + (("?" + u.query) if u.query else "")
    t0 = time.perf_counter()
    conn = None
    try:
        # IPv4 only: avoids the IPv6-first fallback delay that wrecked the Windows local trial.
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
        conn.request("GET", target, headers={"Connection": "close",
                                             "User-Agent": "coldstart-measure/3"})
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
        except ValueError:
            row["error"] = "non-JSON body"
    except Exception as e:  # noqa: BLE001
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        row["error"] = repr(e)
    finally:
        if conn is not None:
            conn.close()
    return row


class Writer:
    def __init__(self, path):
        try:
            new = open(path).read(1) == ""
        except FileNotFoundError:
            new = True
        self.f = open(path, "a", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=FIELDS)
        if new:
            self.w.writeheader()
        self.lock = threading.Lock()

    def write(self, row):
        row = {"timestamp_utc": now_utc(), **row}
        with self.lock:
            self.w.writerow(row)
            self.f.flush()
            idle = row.get("actual_idle_s", "")
            print(f"{row['timestamp_utc'][11:19]} {row['service']:<22} {row['scenario']:<12} "
                  f"#{row['iteration']:<3} idle={idle if idle == '' else format(idle, '.1f')+'s':<9} "
                  f"{row['latency_ms']:>9} ms cold={row['cold']!s:<5} inst={row['instance_id']} "
                  f"{row['error']}", flush=True)


def sleep_until(mono_target, stop):
    while not stop.is_set():
        left = mono_target - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 1.0))


def run_service(writer, svc, reps, path, warmups, stop):
    idle_min, name, base = svc
    url = base.rstrip("/") + path
    scen = f"idle_{idle_min:g}min"
    common = {"variant": "scale-zero", "service": name, "idle_min": idle_min}
    for w in range(1, warmups + 1):
        writer.write({**common, "scenario": "warmup", "iteration": w,
                      "request_start_utc": now_utc(), **timed_get(url)})
    prev_end_mono, prev_end_utc = time.monotonic(), now_utc()
    for i in range(1, reps + 1):
        sleep_until(prev_end_mono + idle_min * 60, stop)
        if stop.is_set():
            return
        start_mono, start_utc = time.monotonic(), now_utc()
        r = timed_get(url)
        end_mono = time.monotonic()
        writer.write({**common, "scenario": scen, "iteration": i,
                      "prev_end_utc": prev_end_utc, "request_start_utc": start_utc,
                      "actual_idle_s": round(start_mono - prev_end_mono, 2), **r})
        prev_end_mono, prev_end_utc = end_mono, now_utc()


def load_services(path):
    out = []
    with open(path) as f:
        for line in f:
            parts = [p.strip() for p in line.strip().split(",")]
            if len(parts) < 3 or not parts[0].replace(".", "", 1).isdigit():
                continue  # header / blank
            out.append((float(parts[0]), parts[1], parts[2]))
    if not out:
        raise SystemExit(f"no services found in {path} (expected lines: idle_min,name,url)")
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--services", default="services3.csv")
    ap.add_argument("--reps", type=int, default=20, help="measured samples per service")
    ap.add_argument("--path", default="/api/report")
    ap.add_argument("--warmups", type=int, default=1)
    ap.add_argument("--out", default="results3.csv")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = ap.parse_args()

    services = load_services(args.services)
    longest = max(s[0] for s in services) * 60 * args.reps
    print("Plan (all services run in parallel):")
    for idle, name, url in services:
        print(f"  idle_{idle:g}min  x{args.reps}  {name}  {url}")
    h, rem = divmod(int(longest), 3600)
    print(f"Estimated total run time: ~{h}h {rem // 60:02d}m (longest tier, plus request time)\n")
    if args.dry_run:
        return

    writer, stop = Writer(args.out), threading.Event()
    threads = [threading.Thread(target=run_service, args=(writer, s, args.reps, args.path, args.warmups, stop))
               for s in services]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.5)
    except KeyboardInterrupt:
        stop.set()
        sys.exit("\nInterrupted (rows written so far are kept in the CSV).")
    print(f"\nDone -> {args.out}")


if __name__ == "__main__":
    main()
