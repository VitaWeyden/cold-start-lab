#!/usr/bin/env python3
"""Analysis of the Test 4 pilot (max-instances=1 vs 3) - and of Test 4 runs in general.

  python3 analyze4.py results4p.csv                     # client side only
  python3 analyze4.py results4p.csv --logs logs4p       # + logs4p_{requests,app,system}.json

Client side (results CSV):
  1. Per arm, FIRST attempts only: n, OK, 429, other failures, 429 rate with 95% Wilson CI,
     cold rate among the OK ones; Fisher exact test max1 vs max3 on the 429 counts.
  2. Per replica and per UTC hour (Test 3: the 429s clustered 14-17 UTC).
  3. Retries: attempts until success and time_to_success_ms (what a user would have waited).
  4. Latency cold vs warm.
With the logs:
  5. Reconciliation: every client attempt vs the Cloud Run request log, joined on the trace id.
     Client attempts missing from the log, log entries that are not ours (other traffic).
  6. Instances: each Cloud Run instance (labels.instanceId), joined with the app's instance_id,
     its start / ready / shutdown, requests served, teardown = shutdown - end of its last response.
     Instances that never served a request are listed (Test 3: one after every 429).
  7. For every failed first attempt: which instance started for it and how long it took.
  8. System log: "Starting new instance" reasons.
Writes summary4p.csv and instances4p.csv (names via --prefix).
"""
import argparse
import csv
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone


# ------------------------------------------------------------------ helpers
def ts(s):
    """ISO timestamp with any number of fractional digits -> aware datetime."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?T\d\d:\d\d:\d\d)(\.\d+)?([+-]\d\d:\d\d)?$", s)
    if not m:
        return datetime.fromisoformat(s)
    frac = (m.group(2) or ".0")[:7]
    t = datetime.fromisoformat(m.group(1) + frac + (m.group(3) or "+00:00"))
    return t.astimezone(timezone.utc)


def wilson(k, n, z=1.96):
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - h) / d) * 100, min(1.0, (c + h) / d) * 100


def fisher_two_sided(a, b, c, d):
    """2x2 table [[a, b], [c, d]] -> two-sided Fisher exact p (sum of tables as or less likely)."""
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2

    def p(x):
        return math.comb(r1, x) * math.comb(r2, c1 - x) / math.comb(n, c1)
    lo, hi = max(0, c1 - r2), min(r1, c1)
    p_obs = p(a)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p_obs * (1 + 1e-9)))


def med(v):
    v = [float(x) for x in v if x not in ("", None)]
    return round(statistics.median(v), 1) if v else ""


def pct(v, q):
    v = sorted(float(x) for x in v if x not in ("", None))
    return round(v[min(len(v) - 1, math.ceil(q * len(v)) - 1)], 1) if v else ""


def is_ok(r):
    return str(r["http_status"]) == "200" and not r["error"]


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def table(rows, hdr):
    w = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(hdr)]
    print("  " + "  ".join(f"{h:>{w[i]}}" for i, h in enumerate(hdr)))
    for r in rows:
        print("  " + "  ".join(f"{str(c):>{w[i]}}" for i, c in enumerate(r)))


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--logs", help="prefix of the collect_logs4.sh files, e.g. logs4p")
    ap.add_argument("--prefix", default="4p", help="suffix for the output files (summary<prefix>.csv)")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv)))
    idle = [r for r in rows if r["scenario"].startswith("idle_")]
    first = [r for r in idle if r["attempt"] == "1"]
    arms = sorted({r["arm"] for r in rows})
    print(f"{a.csv}: {len(rows)} rows, {len(first)} measured samples (first attempts), arms {arms}")
    t_rows = sorted(ts(r["request_start_utc"]) for r in rows)
    print(f"client window (UTC): {t_rows[0]:%Y-%m-%d %H:%M:%S} -> {t_rows[-1]:%H:%M:%S}")

    # 1. per arm ---------------------------------------------------------------
    print("\n1. First attempts per arm")
    hdr = ["arm", "n", "ok", "http_429", "other_fail", "rate_429_pct", "ci95_lo", "ci95_hi",
           "cold_of_ok", "median_cold_ms", "median_warm_ms"]
    out, counts = [], {}
    for arm in arms:
        v = [r for r in first if r["arm"] == arm]
        ok = [r for r in v if is_ok(r)]
        k429 = sum(1 for r in v if str(r["http_status"]) == "429")
        other = len(v) - len(ok) - k429
        lo, hi = wilson(k429, len(v))
        cold = [r for r in ok if r["cold"] == "True"]
        warm = [r for r in ok if r["cold"] == "False"]
        counts[arm] = (k429, len(v) - k429)
        out.append([arm, len(v), len(ok), k429, other, round(100 * k429 / len(v), 1) if v else "",
                    round(lo, 1), round(hi, 1), f"{len(cold)}/{len(ok)}",
                    med(r["latency_ms"] for r in cold), med(r["latency_ms"] for r in warm)])
    table(out, hdr)
    with open(f"summary{a.prefix}.csv", "w", newline="") as f:
        csv.writer(f).writerows([hdr] + out)
    if len(arms) == 2:
        (x1, y1), (x2, y2) = counts[arms[0]], counts[arms[1]]
        p = fisher_two_sided(x1, y1, x2, y2)
        print(f"  Fisher exact (429 vs not, {arms[0]} vs {arms[1]}): two-sided p = {p:.4f}")

    # 2. replica / hour --------------------------------------------------------
    print("\n2. 429 on first attempts per replica and per UTC hour")
    rep = defaultdict(lambda: [0, 0])
    for r in first:
        rep[(r["arm"], r["replica"])][0] += str(r["http_status"]) == "429"
        rep[(r["arm"], r["replica"])][1] += 1
    table([[arm, rp, f"{k}/{n}"] for (arm, rp), (k, n) in sorted(rep.items())], ["arm", "replica", "429/n"])
    hours = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in first:
        hh = ts(r["request_start_utc"]).strftime("%H")
        hours[hh][r["arm"]][0] += str(r["http_status"]) == "429"
        hours[hh][r["arm"]][1] += 1
    table([[hh] + [f"{hours[hh][arm][0]}/{hours[hh][arm][1]}" for arm in arms] for hh in sorted(hours)],
          ["hour_utc"] + arms)

    # 3. retries ---------------------------------------------------------------
    print("\n3. Samples whose first attempt failed")
    series = defaultdict(list)
    for r in idle:
        series[r["first_request_id"]].append(r)
    failed_series = [sorted(s, key=lambda r: int(r["attempt"])) for s in series.values()
                     if not is_ok(min(s, key=lambda r: int(r["attempt"])))]
    if not failed_series:
        print("  none")
    else:
        res = []
        for s in sorted(failed_series, key=lambda s: s[0]["request_start_utc"]):
            last = s[-1]
            res.append([s[0]["request_start_utc"][11:19], s[0]["service"], s[0]["iteration"],
                        " ".join(str(r["http_status"] or "ERR") for r in s), len(s),
                        last["time_to_success_ms"] or "never", last["cold"], last["instance_id"]])
        table(res, ["start_utc", "service", "iter", "statuses", "attempts", "time_to_success_ms",
                    "final_cold", "final_instance"])
        tts = [s[-1]["time_to_success_ms"] for s in failed_series if s[-1]["time_to_success_ms"]]
        print(f"  {len(failed_series)} failed first attempts, {len(tts)} succeeded on a retry; "
              f"time to success median {med(tts)} ms, max {max(map(float, tts)) if tts else ''} ms")

    # 4. latency ---------------------------------------------------------------
    print("\n4. Latency split of successful first attempts, median ms")
    parts = ["latency_ms", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "body_ms", "startup_ms", "init_ms", "work_ms"]
    lat_rows = []
    for arm in arms:
        for label, flag in (("cold", "True"), ("warm", "False")):
            sel = [r for r in first if r["arm"] == arm and is_ok(r) and r["cold"] == flag]
            if sel:
                lat_rows.append([arm, label, len(sel)] + [med(r[p] for r in sel) for p in parts] +
                                [pct((r["latency_ms"] for r in sel), 0.95)])
    table(lat_rows, ["arm", "kind", "n"] + parts + ["p95_latency"])

    # the echo check: did the app see the id we sent?
    ok_all = [r for r in rows if is_ok(r)]
    echoed = sum(1 for r in ok_all if r.get("echo_request_id") == r["request_id"])
    print(f"  request id echoed by the app: {echoed}/{len(ok_all)} successful attempts")

    if not a.logs:
        print("\n(no --logs given: sections 5-8 skipped)")
        return
    reqlog = load_json(f"{a.logs}_requests.json")
    applog = load_json(f"{a.logs}_app.json")
    syslog = load_json(f"{a.logs}_system.json")

    # 5. reconciliation ---------------------------------------------------------
    print("\n5. Client attempts vs Cloud Run request log (joined on trace id)")
    client = {r["request_id"]: r for r in rows}
    if reqlog is None:
        print("  request log file missing")
        by_trace = {}
    else:
        by_trace, foreign = {}, []
        for e in reqlog:
            tr = (e.get("trace") or "").rsplit("/", 1)[-1]
            if tr in client:
                by_trace[tr] = e
            else:
                foreign.append(e)
        missing = [rid for rid in client if rid not in by_trace]
        mism = [rid for rid, e in by_trace.items()
                if str(e.get("httpRequest", {}).get("status", "")) != str(client[rid]["http_status"])]
        print(f"  client attempts {len(client)}, request-log entries {len(reqlog)}: "
              f"matched {len(by_trace)}, client-only {len(missing)}, log-only {len(foreign)}, "
              f"status mismatch {len(mism)}")
        if by_trace == {} and reqlog:
            print("  !! no entry matched: check whether Cloud Run kept our X-Cloud-Trace-Context "
                  "(look at the 'trace' field of a few entries)")
        for e in foreign[:15]:
            h = e.get("httpRequest", {})
            print(f"    not ours: {e.get('timestamp', '')[:19]} {e['resource']['labels'].get('service_name')} "
                  f"{h.get('status')} {h.get('requestMethod')} {h.get('requestUrl', '')[-40:]} "
                  f"ua={h.get('userAgent', '')[:40]} ip={h.get('remoteIp', '')}")
        for rid in missing[:10]:
            r = client[rid]
            print(f"    client-only: {r['request_start_utc'][11:19]} {r['service']} status={r['http_status']} {r['error'][:50]}")

    # 6. instances ---------------------------------------------------------------
    print("\n6. Instances (Cloud Run labels.instanceId <-> app instance_id)")
    inst = defaultdict(lambda: {"service": "", "app_id": "", "start": None, "ready": None,
                                "shutdown": None, "uvicorn_shutdown": None, "startup_ms": "",
                                "served": 0, "first_log": None})
    if applog is None:
        print("  app log file missing")
    else:
        for e in applog:
            iid = e.get("labels", {}).get("instanceId", "")
            if not iid:
                continue
            d = inst[iid]
            t = ts(e.get("timestamp"))
            d["service"] = e["resource"]["labels"].get("service_name", "")
            d["first_log"] = t if d["first_log"] is None else min(d["first_log"], t)
            j = e.get("jsonPayload") or {}
            ev = j.get("event")
            if j.get("instance_id"):
                d["app_id"] = j["instance_id"]
            if ev == "instance_start":
                d["start"] = ts(j.get("process_start_utc")) or t
                d["startup_ms"] = j.get("startup_ms", "")
            elif ev == "instance_ready":
                d["ready"] = t
            elif ev == "instance_shutdown":
                d["shutdown"] = t
            elif ev == "request":
                d["served"] += 1
            if "Shutting down" in (e.get("textPayload") or ""):
                d["uvicorn_shutdown"] = t
        # last response end per app instance id, from the client rows
        last_end = {}
        for r in ok_all:
            t = ts(r["timestamp_utc"])
            if r["instance_id"] and (r["instance_id"] not in last_end or t > last_end[r["instance_id"]]):
                last_end[r["instance_id"]] = t
        hdr = ["service", "instanceId", "app_id", "process_start", "ready", "startup_ms", "served",
               "shutdown", "teardown_after_s"]
        out = []
        for iid, d in sorted(inst.items(), key=lambda kv: (kv[1]["service"], kv[1]["first_log"])):
            sd = d["uvicorn_shutdown"] or d["shutdown"]
            le = last_end.get(d["app_id"])
            out.append([d["service"], iid[-12:], d["app_id"],
                        d["start"].strftime("%H:%M:%S.%f")[:12] if d["start"] else "",
                        d["ready"].strftime("%H:%M:%S.%f")[:12] if d["ready"] else "",
                        d["startup_ms"], d["served"],
                        sd.strftime("%H:%M:%S.%f")[:12] if sd else "",
                        round((sd - le).total_seconds(), 1) if (sd and le) else ""])
        with open(f"instances{a.prefix}.csv", "w", newline="") as f:
            csv.writer(f).writerows([hdr] + out)
        idle_inst = [o for o in out if o[6] == 0]
        td = [o[8] for o in out if o[8] != ""]
        print(f"  {len(out)} instances, {len(idle_inst)} never served a request")
        if td:
            print(f"  teardown after last response (exact instance match): min {min(td)} s, "
                  f"median {statistics.median(td)} s, max {max(td)} s, n={len(td)}")
        for arm in arms:
            names = {r["service"] for r in rows if r["arm"] == arm}
            print(f"    {arm}: {sum(1 for o in out if o[0] in names)} instances, "
                  f"{sum(1 for o in idle_inst if o[0] in names)} never served")
        print(f"  -> instances{a.prefix}.csv")

        # 7. what happened behind each failed first attempt ------------------------
        print("\n7. Failed first attempts: instance started around them")
        if not failed_series:
            print("  none")
        for s in sorted(failed_series, key=lambda s: s[0]["request_start_utc"]):
            r0 = s[0]
            t0 = ts(r0["request_start_utc"])
            near = [(iid, d) for iid, d in inst.items() if d["service"] == r0["service"] and d["start"]
                    and -5 <= (d["start"] - t0).total_seconds() <= 120]
            fin = s[-1]["instance_id"]
            desc = "; ".join(
                f"{d['app_id'] or iid[-8:]} process +{(d['start'] - t0).total_seconds():.1f}s"
                + (f" ready +{(d['ready'] - t0).total_seconds():.1f}s" if d["ready"] else "")
                + f" served {d['served']}" + (" (answered the retry)" if fin and d["app_id"] == fin else "")
                for iid, d in near) or "no instance started within 2 min"
            print(f"  {r0['request_start_utc'][11:19]} {r0['service']} #{r0['iteration']} "
                  f"status {r0['http_status']} after {float(r0['latency_ms']) / 1000:.1f}s: {desc}")

    # 8. system log -----------------------------------------------------------------
    print("\n8. Cloud Run system log")
    if syslog is None:
        print("  system log file missing")
    else:
        reasons = Counter()
        per_svc = Counter()
        for e in syslog:
            txt = e.get("textPayload") or json.dumps(e.get("jsonPayload", ""))
            if "Starting new instance" in txt:
                per_svc[e["resource"]["labels"].get("service_name", "")] += 1
                reasons[txt.split("Reason:", 1)[-1].strip()[:110] if "Reason:" in txt else txt[:110]] += 1
            else:
                reasons["(other) " + txt[:100]] += 1
        print(f"  {len(syslog)} entries; 'Starting new instance' per service: {dict(sorted(per_svc.items()))}")
        for k, n in reasons.most_common(12):
            print(f"    {n:>4}  {k}")

    print("\nDecision rule (written before the run, Test4_report.md section 5):")
    if len(arms) == 2 and "max1" in counts and "max3" in counts:
        k1, k3 = counts["max1"][0], counts["max3"][0]
        p = fisher_two_sided(counts["max1"][0], counts["max1"][1], counts["max3"][0], counts["max3"][1])
        if k1 == 0 and k3 == 0:
            print("  -> case C: no 429 in either arm; the cause is not reproduced. Test 4 keeps max=1 "
                  "(comparable with Tests 1-3) and keeps the retry logic.")
        elif p < 0.05 and k1 > k3:
            print(f"  -> case A: 429 depends on max-instances (max1 {k1}, max3 {k3}, p={p:.3f}). "
                  "Test 4 uses max-instances=3.")
        else:
            print(f"  -> case B: 429 in both arms or no significant difference (max1 {k1}, max3 {k3}, "
                  f"p={p:.3f}). max-instances is not shown to be the cause; Test 4 keeps max=1, "
                  "the 429 stays a reported failure mode.")


if __name__ == "__main__":
    main()