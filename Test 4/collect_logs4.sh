#!/usr/bin/env bash
# Test 4 / pilot: downloads the three log streams that analyze4.py joins with the client CSV.
#   logs4p_requests.json  Cloud Run request log (one entry per request incl. 429s, with trace id)
#   logs4p_app.json       app stdout/stderr (instance_start / identity / ready / request / shutdown,
#                         and uvicorn "Shutting down")
#   logs4p_system.json    Cloud Run system log ("Starting new instance. Reason: ...", shutdowns)
#
#   export PROJECT_ID=cold-start-lab
#   START=2026-10-30T13:50:00Z END=2026-10-30T16:40:00Z ./collect_logs4.sh
# START/END: a few minutes around the window in run4p_meta.txt (END must include the last
# shutdown, i.e. ~16 min after the last request). Logs are kept 30 days by default.
# Run it from the "Test 4" folder (Test 3 lesson: the first teardown file landed elsewhere);
# the script also cd's there itself, so the files always land next to results4p.csv.
set -euo pipefail
PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
START="${START:?Set START (UTC, e.g. 2026-10-30T13:50:00Z)}"
END="${END:?Set END (UTC)}"
PREFIX="${PREFIX:-coldstart-p4-}"
OUT="${OUT:-logs4p}"
cd "$(dirname "$0")"

base="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=~\"^${PREFIX}\" AND timestamp>=\"${START}\" AND timestamp<=\"${END}\""
read_logs() {  # $1 = extra filter, $2 = output file
  gcloud logging read "${base} AND $1" --project "$PROJECT_ID" --order=asc --limit=100000 --format=json > "$2"
  python3 -c 'import json,sys; print(f"{sys.argv[1]}: {len(json.load(open(sys.argv[1])))} entries")' "$2"
}
read_logs 'logName:"run.googleapis.com%2Frequests"' "${OUT}_requests.json"
read_logs '(logName:"run.googleapis.com%2Fstdout" OR logName:"run.googleapis.com%2Fstderr")' "${OUT}_app.json"
read_logs 'logName:"run.googleapis.com%2Fvarlog%2Fsystem"' "${OUT}_system.json"
echo "Next: python3 analyze4.py results4p.csv --logs ${OUT}"