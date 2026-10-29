#!/usr/bin/env bash
# One-time setup of the Cloud Billing export to BigQuery (needed for per-service cost in Test 4).
# Test 3 lesson: creating the dataset is NOT enough. The export itself must be switched on in
# the Console, and it is not backfilled, so do this at least a day before the measurement.
#
#   export PROJECT_ID=cold-start-lab
#   ./setup_billing_export.sh          # creates the dataset, prints the Console steps
#   ./setup_billing_export.sh check    # only checks whether the export table exists yet
set -euo pipefail
PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
DATASET="${DATASET:-billing_export}"
LOCATION="${LOCATION:-EU}"

check() {
  local t
  t="$(bq ls --format=json --max_results=1000 "${PROJECT_ID}:${DATASET}" 2>/dev/null \
       | python3 -c 'import json,sys; print("\n".join(x["tableReference"]["tableId"] for x in json.load(sys.stdin)))' \
       | grep '^gcp_billing_export_v1_' || true)"
  if [ -n "$t" ]; then
    echo "OK: export table exists: ${PROJECT_ID}:${DATASET}.${t}"
    bq query --nouse_legacy_sql --quiet \
      "SELECT MAX(export_time) AS last_export, COUNT(*) AS rows_ FROM \`${PROJECT_ID}.${DATASET}.${t}\`" || true
    return 0
  fi
  echo "NOT YET: no gcp_billing_export_v1_* table in ${PROJECT_ID}:${DATASET}."
  echo "         Either the export is not switched on, or the first rows have not arrived (can take hours)."
  return 1
}

if [ "${1:-}" = "check" ]; then check; exit $?; fi

gcloud services enable bigquery.googleapis.com bigquerydatatransfer.googleapis.com --project "$PROJECT_ID"
bq --location="$LOCATION" mk --dataset --description "Cloud Billing export (cold-start-lab)" \
  "${PROJECT_ID}:${DATASET}" 2>/dev/null || echo "(dataset ${DATASET} already exists)"

BA="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' 2>/dev/null || true)"
cat <<EOF

Dataset ready: ${PROJECT_ID}:${DATASET} (${LOCATION}).

Now the step that was missed in Test 3 - it can only be done in the Console:
  1. https://console.cloud.google.com/billing  -> billing account ${BA:-<yours>}
  2. Billing export -> BigQuery export -> "Standard usage cost" -> EDIT SETTINGS
  3. Project: ${PROJECT_ID}, Dataset: ${DATASET} -> SAVE
     (needs Billing Account Administrator or Billing Account Costs Manager)
  4. The card must now say "Enabled". Take a screenshot for the report.

Then wait until the table appears (usually a few hours) and check:
  ./setup_billing_export.sh check
measure4.py refuses to start until this check passes.
EOF
