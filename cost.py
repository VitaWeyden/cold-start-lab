#!/usr/bin/env python3
"""Monthly cost estimate for the two configurations (Cloud Run, request-based billing).

WARNING: the prices below are starting values only. Before submitting, VERIFY
them against the price list valid at the time of the measurement:
https://cloud.google.com/run/pricing
and state the date in the paper. Finally, compare the estimate with the Billing report.

Region tier: europe-central2 (Warsaw), where this experiment runs, is a TIER 2 region.
The public price list (https://cloud.google.com/run/pricing, checked 2026-10-29) shows one
"Default" USD price per resource and only lists which regions are Tier 1 / Tier 2. Tier 2
request-based prices are 1.4x the Tier 1 ones (dev.to "What Cloud Run costs, with worked
examples", prices read 2026-10-12), and our own bill agrees: Test 1 min-instance CPU
EUR 0.13 / 40,854.6 s and Tests 1+2 EUR 0.52 / 167,813 s per SKU give 1.33-1.44x the Tier 1
idle price at 0.92 EUR/USD (the range comes from the bill rounding to cents).
--tier 2 (the default) applies --tier2-multiplier (default 1.4) to every unit price, incl.
requests. The exact per-SKU prices in EUR are in the Cloud Billing Catalog, e.g.
  curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://cloudbilling.googleapis.com/v1/services/152E-C115-5142/skus?currencyCode=EUR" \
    | python3 -c 'import json,sys; [print(s["description"], s["pricingInfo"][0]["pricingExpression"]["tieredRates"][-1]["unitPrice"]) for s in json.load(sys.stdin)["skus"] if "Tier 2" in s["description"]]'
(152E-C115-5142 = Cloud Run; check the id in the catalog if it returns nothing) - prefer those,
or the BigQuery billing export, over the multiplier when quoting prices in the paper.
The free tier is the same in both tiers (it is based on us-central1 prices).

Usage:
  python3 cost.py --requests-per-month 100000 --avg-ms 40                 # Tier 2 (default)
  python3 cost.py --requests-per-month 100000 --avg-ms 40 --tier 1        # the old Tier 1 estimate
  python3 cost.py --requests-per-month 100000 --avg-ms 40 --no-free-tier
  python3 cost.py --requests-per-month 100000 --avg-ms 40 --eur-rate 0.92

The price list itself is published in USD; --eur-rate converts the USD total
using a flat exchange rate you supply (GCP's actual EUR invoice uses its own
rate at billing time, so treat this as an approximation for comparing against
the Billing report, not an exact prediction). Check the current rate, e.g.
https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates
"""
import argparse

# --- PRICES (USD, "Tier 1" region, e.g. europe-west1) - VERIFY! Tier 2 = these x multiplier ---
PRICE_CPU_ACTIVE_PER_VCPU_S = 0.000024      # while a request is being processed
PRICE_MEM_ACTIVE_PER_GIB_S = 0.0000025
PRICE_CPU_IDLE_PER_VCPU_S = 0.0000025       # idle time of a min-instance
PRICE_MEM_IDLE_PER_GIB_S = 0.0000025
PRICE_PER_MILLION_REQUESTS = 0.40
# Monthly free tier (applied to the account, not per instance)
FREE_CPU_S, FREE_MEM_GIB_S, FREE_REQUESTS = 180_000, 360_000, 2_000_000

SECONDS_PER_MONTH = 30 * 24 * 3600


def cost(requests, avg_ms, vcpu, mem_gib, min_instances, free_tier, mult=1.0):
    active_s = requests * avg_ms / 1000            # assumption: requests do not overlap
    cpu_s = active_s * vcpu
    mem_s = active_s * mem_gib
    req = requests

    idle_s = max(0.0, SECONDS_PER_MONTH - active_s) if min_instances >= 1 else 0.0
    idle_cpu_s = idle_s * vcpu * min_instances
    idle_mem_s = idle_s * mem_gib * min_instances

    if free_tier:
        cpu_s = max(0, cpu_s - FREE_CPU_S)
        mem_s = max(0, mem_s - FREE_MEM_GIB_S)
        req = max(0, req - FREE_REQUESTS)

    parts = {
        "active CPU": cpu_s * PRICE_CPU_ACTIVE_PER_VCPU_S * mult,
        "active memory": mem_s * PRICE_MEM_ACTIVE_PER_GIB_S * mult,
        "requests": req / 1e6 * PRICE_PER_MILLION_REQUESTS * mult,
        "idle CPU (min-instance)": idle_cpu_s * PRICE_CPU_IDLE_PER_VCPU_S * mult,
        "idle memory (min-instance)": idle_mem_s * PRICE_MEM_IDLE_PER_GIB_S * mult,
    }
    return parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-per-month", type=int, required=True)
    ap.add_argument("--avg-ms", type=float, required=True, help="average request processing time in ms")
    ap.add_argument("--vcpu", type=float, default=1.0)
    ap.add_argument("--memory-gib", type=float, default=0.5)
    ap.add_argument("--no-free-tier", action="store_true")
    ap.add_argument("--eur-rate", type=float, default=None,
                     help="also print totals converted to EUR at this flat USD->EUR rate (e.g. 0.92)")
    ap.add_argument("--tier", type=int, choices=(1, 2), default=2,
                    help="region price tier; europe-central2 (this experiment) is Tier 2 (default)")
    ap.add_argument("--tier2-multiplier", type=float, default=1.4,
                    help="Tier 2 price = Tier 1 price x this (default 1.4, see the docstring)")
    a = ap.parse_args()
    mult = a.tier2_multiplier if a.tier == 2 else 1.0

    print(f"Scenario: {a.requests_per_month:,} requests/month, {a.avg_ms} ms/request, "
          f"{a.vcpu} vCPU, {a.memory_gib} GiB, free tier: {'no' if a.no_free_tier else 'yes'}, "
          f"Tier {a.tier} prices" + (f" (Tier 1 x {mult})" if a.tier == 2 else "") + "\n")
    totals = {}
    for name, m in (("scale-zero (min=0)", 0), ("always-warm (min=1)", 1)):
        parts = cost(a.requests_per_month, a.avg_ms, a.vcpu, a.memory_gib, m, not a.no_free_tier, mult)
        totals[name] = sum(parts.values())
        print(name)
        for k, v in parts.items():
            line = f"  {k:<32} ${v:>9.4f}"
            if a.eur_rate:
                line += f"   (~EUR {v * a.eur_rate:>7.4f})"
            print(line)
        total_line = f"  {'TOTAL':<32} ${totals[name]:>9.4f} / month"
        if a.eur_rate:
            total_line += f"   (~EUR {totals[name] * a.eur_rate:>7.4f} / month)"
        print(total_line + "\n")
    diff = totals["always-warm (min=1)"] - totals["scale-zero (min=0)"]
    diff_line = f"Extra cost of the warm instance: ${diff:.2f} / month"
    if a.eur_rate:
        diff_line += f"  (~EUR {diff * a.eur_rate:.2f} / month, at rate {a.eur_rate})"
    print(diff_line)


if __name__ == "__main__":
    main()