#!/usr/bin/env python3
"""Analysis of Test 4: three application sizes (S, M, L) x six idle tiers (5 - 20 min).

Implements the rules fixed before the run in Test4_report.md section 5 (numbers in brackets).
Derived from the pilot's analyze4.py ("Test 4 pilot/"): the log joins are the same.
Standard library only (runs in Cloud Shell and on the VM as is).

  python3 analyze4.py results4.csv                                   # client side only
  python3 analyze4.py results4.csv --logs logs4                      # + logs4_{requests,app,system}.json
  python3 analyze4.py results4.csv --logs logs4 --billing billing4.csv

Client side (results CSV, first attempts of measured samples; warm-ups excluded):
  1. Plumbing: app_size = services file size, request id echoed.            [6.5]
  2. Cold rate per size x tier (Wilson CI); Fisher S vs L at 14, 15, 16 min. [5.3]
  3. Cold-start latency per size; pairwise Mann-Whitney (Holm) AND the
     service-level separation of the 16 / 18 / 20 min services.            [5.1]
  4. Which part grows: start-up phases and platform_ms per size.           [5.2]
  5. Warm latency at 5 min per size.                                        [5.4]
  6. Failures (429 / 5xx / errors) per size and hour, retries.              [5.6]
  7. Overlapping cold starts: medians with / without them.                  [5.6]
With the logs:
  8. Reconciliation client <-> request log on the trace id.                 [6.4]
  9. Instances, teardown per size (shift rule), early teardowns, OOM hints. [5.3, 5.6, 6.6]
 10. System log: "Starting new instance" and memory-related entries.
With the billing CSV (billing_query4.sql):
 11. Billed requests vs client attempts, vCPU-s and EUR per cold start.     [5.5, 6.4]
Writes summary4.csv (size x tier), cold4.csv (one row per cold sample) and instances4.csv.
"""
import argparse
import csv
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

SIZES = ["s", "m", "l"]
COLD_TIERS = (16.0, 18.0, 20.0)      # cold every time in Test 3: the service-level rule uses these
BOUNDARY_TIERS = (14.0, 15.0, 16.0)
WARM_TIER = 5.0
EARLY_TEARDOWN_S = 800               # teardowns below this are listed, not used in the medians
SHIFT_MIN_S = 10                     # [5.3] a teardown shift must be at least this large
PLATFORM_GROWTH_MS = 500             # [5.2]
WARM_DIFF_MS = 100                   # [5.4]
OVERLAP_DIFF = 0.10                  # [5.6]
COLD_KEYS = ["request_start_utc", "service", "size", "idle_min", "iteration", "latency_ms", "ttfb_ms",
             "startup_ms", "imports_ms", "init_ms", "ml_import_ms", "model_load_ms", "work_ms",
             "platform_ms", "overlapping", "instance_id", "cloud_run_instance_id"]


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


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def vals(rows, key):
    return [v for v in (num(r.get(key)) for r in rows) if v is not None]


def med(v):
    v = [float(x) for x in v if x not in ("", None)]
    return round(statistics.median(v), 1) if v else ""


def pct(v, q):
    v = sorted(float(x) for x in v if x not in ("", None))
    return round(v[min(len(v) - 1, math.ceil(q * len(v)) - 1)], 1) if v else ""


def wilson(k, n, z=1.96):
    if n == 0:
        return "", ""
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(max(0.0, (c - h) / d) * 100, 1), round(min(1.0, (c + h) / d) * 100, 1)


def fisher_two_sided(a, b, c, d):
    """2x2 table [[a, b], [c, d]] -> two-sided Fisher exact p."""
    r1, r2, c1 = a + b, c + d, a + c
    n = r1 + r2
    if n == 0:
        return float("nan")

    def p(x):
        return math.comb(r1, x) * math.comb(r2, c1 - x) / math.comb(n, c1)
    lo, hi = max(0, c1 - r2), min(r1, c1)
    p_obs = p(a)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p_obs * (1 + 1e-9)))


def mann_whitney(x, y):
    """Two-sided Mann-Whitney U, normal approximation with tie correction -> (U_x, p)."""
    nx, ny = len(x), len(y)
    if nx == 0 or ny == 0:
        return "", float("nan")
    allv = sorted([(v, 0) for v in x] + [(v, 1) for v in y])
    ranks, i, ties = [0.0] * len(allv), 0, 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    rx = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u = rx - nx * (nx + 1) / 2
    n = nx + ny
    mu = nx * ny / 2
    sd = math.sqrt(nx * ny / 12 * ((n + 1) - ties / (n * (n - 1)))) if n > 1 else 0
    if sd == 0:
        return u, 1.0
    z = (abs(u - mu) - 0.5) / sd
    return u, min(1.0, math.erfc(max(z, 0) / math.sqrt(2)))


def holm(ps):
    """dict name -> p  =>  dict name -> Holm-adjusted p."""
    items = sorted((p, k) for k, p in ps.items() if not math.isnan(p))
    m, out, run = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    for k, p in ps.items():
        out.setdefault(k, float("nan"))
    return out


def fmt_p(p):
    return "n/a" if p is None or (isinstance(p, float) and math.isnan(p)) else (
        "<0.0001" if p < 1e-4 else f"{p:.4f}")


def is_ok(r):
    return str(r["http_status"]) == "200" and not r["error"]


def is_cold(r):
    return str(r.get("cold")) == "True"


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


def tier(r):
    return float(r["idle_min"])


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--logs", help="prefix of the collect_logs4.sh files, e.g. logs4")
    ap.add_argument("--billing", help="CSV written from billing_query4.sql")
    ap.add_argument("--prefix", default="4", help="suffix of the output files (summary<prefix>.csv)")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv)))
    idle = [r for r in rows if r["scenario"].startswith("idle_")]
    first = [r for r in idle if r["attempt"] == "1"]
    ok_first = [r for r in first if is_ok(r)]
    sizes = [s for s in SIZES if any(r["size"] == s for r in rows)]
    tiers = sorted({tier(r) for r in rows})
    services = sorted({r["service"] for r in rows})
    size_of = {r["service"]: r["size"] for r in rows}
    tier_of = {r["service"]: tier(r) for r in rows}
    t_rows = sorted(ts(r["request_start_utc"]) for r in rows)
    print(f"{a.csv}: {len(rows)} rows, {len(first)} measured samples (first attempts), "
          f"{len(services)} services, sizes {sizes}, tiers {[f'{t:g}' for t in tiers]}")
    print(f"client window (UTC): {t_rows[0]:%Y-%m-%d %H:%M:%S} -> {t_rows[-1]:%H:%M:%S}")

    # 1. plumbing on the client side ------------------------------------------------------
    print("\n1. Plumbing (client side)  [6.5]")
    ok_all = [r for r in rows if is_ok(r)]
    wrong = [r for r in ok_all if r.get("app_size") != r["size"]]
    echoed = sum(1 for r in ok_all if r.get("echo_request_id") == r["request_id"])
    print(f"  app_size = services file size: {len(ok_all) - len(wrong)}/{len(ok_all)} successful attempts")
    for r in wrong[:5]:
        print(f"    !! {r['service']} expects {r['size']}, the app says {r.get('app_size')!r}")
    print(f"  request id echoed by the app: {echoed}/{len(ok_all)}")
    if ok_all:
        for s in sizes:
            sel = [r for r in ok_all if r["size"] == s]
            print(f"  size {s}: model_mb {med(vals(sel, 'model_mb'))}, rss_mb median {med(vals(sel, 'rss_mb'))}, "
                  f"peak_rss_mb max {max(vals(sel, 'peak_rss_mb'), default='')}")

    # 2. cold rate per size x tier ------------------------------------------------------
    print("\n2. Cold rate of successful first attempts per size x tier  [5.3]")
    hdr = ["size", "tier_min", "n", "ok", "fail", "cold", "cold_pct", "ci95_lo", "ci95_hi",
           "median_latency_ms", "p95_latency_ms"]
    summ = []
    cold_counts = {}
    for s in sizes:
        for t in tiers:
            v = [r for r in first if r["size"] == s and tier(r) == t]
            if not v:
                continue
            ok = [r for r in v if is_ok(r)]
            k = sum(1 for r in ok if is_cold(r))
            lo, hi = wilson(k, len(ok))
            cold_counts[(s, t)] = (k, len(ok) - k)
            summ.append([s, f"{t:g}", len(v), len(ok), len(v) - len(ok), k,
                         round(100 * k / len(ok), 1) if ok else "", lo, hi,
                         med(vals(ok, "latency_ms")), pct(vals(ok, "latency_ms"), 0.95)])
    table(summ, hdr)
    with open(f"summary{a.prefix}.csv", "w", newline="") as f:
        csv.writer(f).writerows([hdr] + summ)
    for t in BOUNDARY_TIERS:
        if ("s", t) in cold_counts and ("l", t) in cold_counts:
            (a1, b1), (a2, b2) = cold_counts[("s", t)], cold_counts[("l", t)]
            print(f"  {t:g} min, S vs L: cold {a1}/{a1 + b1} vs {a2}/{a2 + b2}, "
                  f"Fisher two-sided p = {fmt_p(fisher_two_sided(a1, b1, a2, b2))}")

    # 3. cold-start latency per size -----------------------------------------------------
    print("\n3. Cold-start latency per size (all cold successful first attempts)  [5.1]")
    cold = [r for r in ok_first if is_cold(r)]
    lat_rows = []
    for s in sizes:
        v = [r for r in cold if r["size"] == s]
        lat_rows.append([s, len(v), med(vals(v, "latency_ms")), pct(vals(v, "latency_ms"), 0.95),
                         med(vals(v, "ttfb_ms")), med(vals(v, "startup_ms"))])
    table(lat_rows, ["size", "n_cold", "median_ms", "p95_ms", "median_ttfb_ms", "median_startup_ms"])

    print("  service medians, cold tiers (16 / 18 / 20 min):")
    svc_med = defaultdict(dict)
    for svc in services:
        if tier_of[svc] in COLD_TIERS:
            v = vals([r for r in cold if r["service"] == svc], "latency_ms")
            if v:
                svc_med[size_of[svc]][svc] = round(statistics.median(v), 1)
    for s in sizes:
        print(f"    {s}: " + ", ".join(f"{k} {v}" for k, v in sorted(svc_med[s].items())))

    pairs = [(x, y) for i, x in enumerate(sizes) for y in sizes[i + 1:]]
    raw = {}
    for x, y in pairs:
        _, raw[(x, y)] = mann_whitney(vals([r for r in cold if r["size"] == x], "latency_ms"),
                                      vals([r for r in cold if r["size"] == y], "latency_ms"))
    adj = holm(raw)
    verdict_rows = []
    for x, y in pairs:
        sx, sy = list(svc_med[x].values()), list(svc_med[y].values())
        if len(sx) >= 3 and len(sy) >= 3:
            sep = min(sy) > max(sx)
            sep_txt = "yes" if sep else "no"
        else:
            sep, sep_txt = False, f"n/a ({len(sx)} vs {len(sy)} services)"
        shown = sep and not math.isnan(adj[(x, y)]) and adj[(x, y)] < 0.05
        mx = med(vals([r for r in cold if r["size"] == x], "latency_ms"))
        my = med(vals([r for r in cold if r["size"] == y], "latency_ms"))
        diff = round(my - mx, 1) if mx != "" and my != "" else ""
        verdict_rows.append([f"{x}-{y}", diff, fmt_p(raw[(x, y)]), fmt_p(adj[(x, y)]), sep_txt,
                             "SHOWN" if shown else "not shown"])
    table(verdict_rows, ["pair", "median_diff_ms", "p_MWU", "p_Holm", "all_services_separated", "difference"])

    # 4. which part grows ----------------------------------------------------------------
    print("\n4. Which part of the cold start grows (medians, ms)  [5.2]")
    for r in cold:
        t, su, wk = num(r["ttfb_ms"]), num(r["startup_ms"]), num(r["work_ms"])
        r["platform_ms"] = round(t - su - wk, 1) if None not in (t, su, wk) else ""
    parts = ["latency_ms", "ttfb_ms", "startup_ms", "imports_ms", "init_ms", "ml_import_ms",
             "model_load_ms", "work_ms", "platform_ms"]
    prow = []
    for s in sizes:
        v = [r for r in cold if r["size"] == s]
        prow.append([s, len(v)] + [med(vals(v, p)) for p in parts])
    table(prow, ["size", "n"] + parts)
    pm = {s: med(vals([r for r in cold if r["size"] == s], "platform_ms")) for s in sizes}
    if pm.get("s", "") != "" and pm.get("l", "") != "":
        g = round(pm["l"] - pm["s"], 1)
        print(f"  platform_ms L - S = {g} ms -> "
              + ("the part outside the app grows by more than "
                 f"{PLATFORM_GROWTH_MS} ms: image size / platform contributes" if g > PLATFORM_GROWTH_MS
                 else f"below {PLATFORM_GROWTH_MS} ms: the growth is the app's own start-up work"))

    # 5. warm latency at 5 min ------------------------------------------------------------
    print(f"\n5. Warm latency at the {WARM_TIER:g} min tier  [5.4]")
    warm = [r for r in ok_first if tier(r) == WARM_TIER and not is_cold(r)]
    wrow = []
    for s in sizes:
        v = [r for r in warm if r["size"] == s]
        wrow.append([s, len(v), med(vals(v, "latency_ms")), pct(vals(v, "latency_ms"), 0.95),
                     med(vals(v, "work_ms"))])
    table(wrow, ["size", "n_warm", "median_ms", "p95_ms", "median_work_ms"])
    wm = [x[2] for x in wrow if x[2] != ""]
    if len(wm) >= 2:
        spread = round(max(wm) - min(wm), 1)
        print(f"  largest difference of the medians: {spread} ms "
              + (f"(> {WARM_DIFF_MS} ms: reported, check work_ms)" if spread > WARM_DIFF_MS else
                 f"(<= {WARM_DIFF_MS} ms: warm requests equal, as expected)"))

    # 6. failures --------------------------------------------------------------------------
    print("\n6. Failed first attempts (429 / 5xx / errors)  [5.6]")
    series = defaultdict(list)
    for r in idle:
        series[r["first_request_id"]].append(r)
    failed = [sorted(s, key=lambda r: int(r["attempt"])) for s in series.values()
              if not is_ok(min(s, key=lambda r: int(r["attempt"])))]
    frow = []
    for s in sizes:
        v = [r for r in first if r["size"] == s]
        k429 = sum(1 for r in v if str(r["http_status"]) == "429")
        k5 = sum(1 for r in v if str(r["http_status"]).startswith("5"))
        kerr = sum(1 for r in v if not str(r["http_status"]))
        lo, hi = wilson(k429 + k5 + kerr, len(v))
        frow.append([s, len(v), k429, k5, kerr, f"{lo}-{hi}"])
    table(frow, ["size", "n", "http_429", "http_5xx", "conn_error", "fail_ci95_pct"])
    if failed:
        hours = defaultdict(lambda: defaultdict(int))
        for sr in failed:
            hours[ts(sr[0]["request_start_utc"]).strftime("%H")][sr[0]["size"]] += 1
        print("  per UTC hour: " + ", ".join(f"{h}: " + "/".join(f"{s}{hours[h][s]}" for s in sizes)
                                            for h in sorted(hours)))
        for sr in sorted(failed, key=lambda s: s[0]["request_start_utc"])[:40]:
            last = sr[-1]
            print(f"    {sr[0]['request_start_utc'][11:19]} {sr[0]['service']} #{sr[0]['iteration']} "
                  f"{' '.join(str(r['http_status'] or 'ERR') for r in sr)} -> "
                  f"time_to_success {last['time_to_success_ms'] or 'never'} ms {sr[0]['error'][:50]}")
    else:
        print("  none")

    # 7. overlapping cold starts -----------------------------------------------------------
    print("\n7. Overlapping cold starts  [5.6]")
    spans = []   # every cold or failed attempt (warm-ups included) = something was starting
    for r in rows:
        if is_cold(r) or not is_ok(r):
            t0, lat = ts(r["request_start_utc"]), num(r["latency_ms"])
            if t0 and lat is not None:
                spans.append((t0, t0 + timedelta(milliseconds=lat), r["service"], r["request_id"]))
    for r in cold:
        t0 = ts(r["request_start_utc"])
        t1 = t0 + timedelta(milliseconds=float(r["latency_ms"]))
        r["overlapping"] = any(s0 < t1 and s1 > t0 and svc != r["service"] for s0, s1, svc, _ in spans)
    orow = []
    for s in sizes:
        v = [r for r in cold if r["size"] == s]
        alone = [r for r in v if not r["overlapping"]]
        m_all, m_alone = med(vals(v, "latency_ms")), med(vals(alone, "latency_ms"))
        d = round(abs(m_all - m_alone) / m_alone, 3) if m_all != "" and m_alone not in ("", 0) else ""
        orow.append([s, len(v), len(v) - len(alone), m_all, m_alone,
                     "" if d == "" else f"{100 * d:.1f}%" + (" REPORT" if d > OVERLAP_DIFF else "")])
    table(orow, ["size", "n_cold", "overlapping", "median_all_ms", "median_without_ms", "difference"])
    with open(f"cold{a.prefix}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLD_KEYS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(cold, key=lambda r: r["request_start_utc"]))
    print(f"  -> cold{a.prefix}.csv")

    # logs ---------------------------------------------------------------------------------
    inst = {}
    if a.logs:
        reqlog = load_json(f"{a.logs}_requests.json")
        applog = load_json(f"{a.logs}_app.json")
        syslog = load_json(f"{a.logs}_system.json")

        # 8. reconciliation
        print("\n8. Client attempts vs Cloud Run request log (trace id)  [6.4]")
        client = {r["request_id"]: r for r in rows}
        if reqlog is None:
            print("  request log file missing")
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
            print(f"  client attempts {len(client)}, request-log entries {len(reqlog)}: matched {len(by_trace)}, "
                  f"client-only {len(missing)}, log-only {len(foreign)}, status mismatch {len(mism)}")
            for e in foreign[:15]:
                h = e.get("httpRequest", {})
                print(f"    not ours: {e.get('timestamp', '')[:19]} {e['resource']['labels'].get('service_name')} "
                      f"{h.get('status')} {h.get('requestUrl', '')[-40:]} ua={h.get('userAgent', '')[:40]} "
                      f"ip={h.get('remoteIp', '')}")
            for rid in missing[:10]:
                r = client[rid]
                print(f"    client-only: {r['request_start_utc'][11:19]} {r['service']} "
                      f"status={r['http_status']} {r['error'][:50]}")

        # 9. instances and teardown
        print("\n9. Instances and teardown  [5.3, 5.6, 6.6]")
        inst = defaultdict(lambda: {"service": "", "app_id": "", "start": None, "ready": None,
                                    "shutdown": None, "uvicorn_shutdown": None, "startup_ms": "",
                                    "rss_mb": "", "served": 0, "first_log": None})
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
                    d["rss_mb"] = j.get("rss_mb", "")
                elif ev == "instance_ready":
                    d["ready"] = t
                elif ev == "instance_shutdown":
                    d["shutdown"] = t
                elif ev == "request":
                    d["served"] += 1
                if "Shutting down" in (e.get("textPayload") or ""):
                    d["uvicorn_shutdown"] = t
            last_end = {}
            for r in ok_all:
                t = ts(r["timestamp_utc"])
                if r["instance_id"] and (r["instance_id"] not in last_end or t > last_end[r["instance_id"]]):
                    last_end[r["instance_id"]] = t
            hdr = ["service", "size", "tier_min", "instanceId", "app_id", "process_start", "ready", "startup_ms",
                   "rss_mb", "served", "shutdown", "teardown_after_s"]
            out = []
            for iid, d in sorted(inst.items(), key=lambda kv: (kv[1]["service"], kv[1]["first_log"])):
                sd = d["uvicorn_shutdown"] or d["shutdown"]
                le = last_end.get(d["app_id"])
                out.append([d["service"], size_of.get(d["service"], ""),
                            f"{tier_of[d['service']]:g}" if d["service"] in tier_of else "", iid[-12:],
                            d["app_id"],
                            d["start"].strftime("%H:%M:%S.%f")[:12] if d["start"] else "",
                            d["ready"].strftime("%H:%M:%S.%f")[:12] if d["ready"] else "",
                            d["startup_ms"], d["rss_mb"], d["served"],
                            sd.strftime("%H:%M:%S.%f")[:12] if sd else "",
                            round((sd - le).total_seconds(), 1) if (sd and le) else ""])
            with open(f"instances{a.prefix}.csv", "w", newline="") as f:
                csv.writer(f).writerows([hdr] + out)
            no_id = sum(1 for o in out if not o[4])
            never = [o for o in out if o[9] == 0]
            no_sd = [o for o in out if not o[10]]
            print(f"  {len(out)} instances: {no_id} without app_id, {len(never)} never served a request, "
                  f"{len(no_sd)} without a shutdown line (possible crash / OOM, or still alive at END)")
            for o in no_sd[:10]:
                print(f"    no shutdown: {o[0]} {o[3]} started {o[5]} served {o[9]}")
            td = defaultdict(list)   # size -> teardown seconds (normal ones)
            early = []               # whole rows of the early ones
            for o in out:
                if o[11] == "":
                    continue
                if o[11] < EARLY_TEARDOWN_S:
                    early.append(o)
                else:
                    td[o[1]].append(o[11])
            trow = []
            for s in sizes:
                v = td[s]
                trow.append([s, len(v), min(v, default=""), med(v), max(v, default=""),
                             sum(1 for x in v if x < 900)])
            table(trow, ["size", "n", "min_s", "median_s", "max_s", "below_900s"])
            raw = {}
            for s in [x for x in sizes if x != "s"]:
                _, raw[s] = mann_whitney(td["s"], td[s])
            adj = holm(raw)
            for s, p in adj.items():
                ms, mx = med(td["s"]), med(td[s])
                diff = round(mx - ms, 1) if ms != "" and mx != "" else ""
                shift = diff != "" and abs(diff) >= SHIFT_MIN_S and p < 0.05
                print(f"  S vs {s.upper()}: median difference {diff} s, MWU p {fmt_p(raw[s])}, Holm {fmt_p(p)} -> "
                      + ("teardown SHIFT shown" if shift else
                         f"no shift shown (needs >= {SHIFT_MIN_S} s and p < 0.05)"))
            if early:
                print(f"  early teardowns (< {EARLY_TEARDOWN_S} s), not in the medians:")
                for o in early:
                    print(f"    {o[0]} {o[3]} process {o[5]} served {o[9]} shutdown {o[10]} after {o[11]} s")
            print(f"  -> instances{a.prefix}.csv")

        # 10. system log
        print("\n10. Cloud Run system log")
        if syslog is None:
            print("  system log file missing")
        else:
            reasons, per_size, memory = Counter(), Counter(), []
            for e in syslog:
                txt = e.get("textPayload") or json.dumps(e.get("jsonPayload", ""))
                svc = e["resource"]["labels"].get("service_name", "")
                if "Starting new instance" in txt:
                    per_size[size_of.get(svc, "?")] += 1
                    reasons[txt.split("Reason:", 1)[-1].strip()[:100] if "Reason:" in txt else txt[:100]] += 1
                else:
                    reasons["(other) " + re.sub(r'"[^"]*"', '"..."', txt)[:100]] += 1
                if re.search(r"memory|OOM|out of memory", txt, re.I):
                    memory.append((e.get("timestamp", "")[:19], svc, txt[:120]))
            print(f"  {len(syslog)} entries; 'Starting new instance' per size: {dict(sorted(per_size.items()))}")
            for k, n in reasons.most_common(12):
                print(f"    {n:>5}  {k}")
            print(f"  memory-related entries: {len(memory)}")
            for m in memory[:10]:
                print(f"    {m[0]} {m[1]} {m[2]}")

    # 11. billing ------------------------------------------------------------------------------
    if a.billing:
        print("\n11. Cost from the billing export  [5.5]")
        b = list(csv.DictReader(open(a.billing)))
        h0 = t_rows[0].replace(minute=0, second=0, microsecond=0)
        h1 = t_rows[-1] + timedelta(minutes=30)       # the last requests' instances shut down later
        inwin = [x for x in b if h0 <= ts(x["hour"].replace(" ", "T")).replace(tzinfo=timezone.utc) <= h1]
        print(f"  {len(b)} rows, {len(inwin)} in the run hours {h0:%Y-%m-%d %H}:00 - {h1:%H}:59 UTC")
        agg = defaultdict(lambda: defaultdict(float))
        for x in inwin:
            key = (x["size"], float(x["idle_min"]))
            amt = float(x["usage_amount"] or 0)
            if x["sku"] == "Requests":
                agg[key]["req"] += amt
            elif "CPU" in x["sku"]:
                agg[key]["vcpu_s"] += amt
            elif "Memory" in x["sku"]:
                agg[key]["gib_s"] += amt / 2**30
            agg[key]["gross"] += float(x["gross_cost_eur"] or 0)
            agg[key]["net"] += float(x["net_cost_eur"] or 0)
        attempts = Counter((r["size"], tier(r)) for r in rows)
        brow = []
        for (s, t), d in sorted(agg.items(), key=lambda kv: (SIZES.index(kv[0][0]) if kv[0][0] in SIZES else 9,
                                                             kv[0][1])):
            brow.append([s, f"{t:g}", int(d["req"]), attempts.get((s, t), 0),
                         "ok" if int(d["req"]) == attempts.get((s, t), 0) else "DIFF",
                         round(d["vcpu_s"], 1), round(d["gib_s"], 1), round(d["gross"], 6), round(d["net"], 6)])
        table(brow, ["size", "tier", "billed_req", "client_attempts", "check", "vcpu_s", "gib_s", "gross_eur", "net_eur"])
        print("  per cold start, cold tiers 16 / 18 / 20 min:")
        crow = []
        for s in sizes:
            req = sum(agg[(s, t)]["req"] for t in COLD_TIERS)
            if not req:
                continue
            vc = sum(agg[(s, t)]["vcpu_s"] for t in COLD_TIERS)
            gr = sum(agg[(s, t)]["gross"] for t in COLD_TIERS)
            v = [r for r in cold if r["size"] == s and tier(r) in COLD_TIERS]
            crow.append([s, int(req), round(vc / req, 2), med(vals(v, "latency_ms")), med(vals(v, "startup_ms")),
                         round(1000 * gr / req, 4)])
        table(crow, ["size", "billed_req", "vcpu_s_per_req", "median_latency_ms", "median_startup_ms",
                     "gross_eur_per_1000"])
        tot_g = sum(d["gross"] for d in agg.values())
        tot_n = sum(d["net"] for d in agg.values())
        print(f"  Test 4 total in the run hours: gross EUR {tot_g:.4f}, net EUR {tot_n:.4f}")


if __name__ == "__main__":
    main()
