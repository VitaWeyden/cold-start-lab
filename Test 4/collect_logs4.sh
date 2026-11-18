#!/usr/bin/env bash
# Test 4: downloads the three log streams that the analysis joins with the client CSV.
#   logs4_requests.json  Cloud Run request log (one entry per request incl. 429s, with trace id)
#   logs4_app.json       app stdout/stderr (instance_start / identity / ready / request / shutdown,
#                        and uvicorn "Shutting down")
#   logs4_system.json    Cloud Run system log ("Starting new instance", startup probe, shutdowns)
# Same script as in the pilot, with the Test 4 defaults (prefix coldstart-t4-, output logs4).
#
#   export PROJECT_ID=cold-start-lab
#   START=2026-11-25T07:55:00Z END=2026-11-25T18:30:00Z ./collect_logs4.sh
# START/END: a few minutes around the window in run4_meta.txt (END must include the last
# shutdown, i.e. ~16 min after the last request). Logs are kept 30 days by default.
# 18 services x 10 h give more entries than the pilot; --limit is raised accordingly.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
START="${START:?Set START (UTC, e.g. 2026-11-25T07:55:00Z)}"
END="${END:?Set END (UTC)}"
PREFIX="${PREFIX:-coldstart-t4-}"
OUT="${OUT:-logs4}"
cd "$(dirname "$0")"

base="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^${PREFIX}\" AND timestamp>=\"${START}\" AND timestamp<=\"${END}\""
read_logs() {  # $1 = extra filter, $2 = output file
  gcloud logging read "${base} AND $1" --project "$PROJECT_ID" --order=asc --limit=500000 --format=json > "$2"
  python3 -c 'import json,sys; print(f"{sys.argv[1]}: {len(json.load(open(sys.argv[1])))} entries")' "$2"
}
read_logs 'logName:"run.googleapis.com%2Frequests"' "${OUT}_requests.json"
read_logs '(logName:"run.googleapis.com%2Fstdout" OR logName:"run.googleapis.com%2Fstderr")' "${OUT}_app.json"
read_logs 'logName:"run.googleapis.com%2Fvarlog%2Fsystem"' "${OUT}_system.json"
echo "Next: python3 analyze4.py results4.csv --logs ${OUT}"
