#!/usr/bin/env bash
# Deletes the resources of the experiment. Always run this when the measurements are done:
# min-instances services, Cloud SQL and Redis (Memorystore) instances and the measurement VM
# keep billing even when nobody uses them.
#
#   export PROJECT_ID=cold-start-lab
#   export REGION=europe-central2
#   ./cleanup.sh                 # everything except the VM, the image repository and the billing dataset
#   KEEP_IMAGES=0 ./cleanup.sh   # also delete the Artifact Registry repository (all images)
#   DELETE_VM=1   ./cleanup.sh   # also delete the measurement VM
#   ONLY=pilot4   ./cleanup.sh   # only the Test 4 pilot services
set -uo pipefail

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-central2}"
ZONE="${ZONE:-${REGION}-a}"
VM="${VM:-coldstart-measurer}"
P=(--project "$PROJECT_ID")

del_run() { gcloud run services delete "$1" --region "$REGION" "${P[@]}" --quiet 2>/dev/null && echo "deleted service $1"; }

# Test 4 pilot (deploy4p.sh)
for r in a b c; do for m in 1 3; do del_run "coldstart-p4-m${m}-${r}"; done; done
[ "${ONLY:-}" = "pilot4" ] && { echo "Pilot services deleted."; exit 0; }

# Test 4 and any other coldstart-* service still around (covers new names too)
for s in $(gcloud run services list --region "$REGION" "${P[@]}" --format='value(metadata.name)' 2>/dev/null | grep '^coldstart-'); do
  del_run "$s"
done

# Cloud SQL (Test 4 size M) - billed per hour while it exists, even with no connections
for i in $(gcloud sql instances list "${P[@]}" --format='value(name)' 2>/dev/null | grep '^coldstart-'); do
  gcloud sql instances delete "$i" "${P[@]}" --quiet && echo "deleted Cloud SQL $i"
done
# Memorystore Redis (only if it was ever created)
for i in $(gcloud redis instances list --region "$REGION" "${P[@]}" --format='value(name)' 2>/dev/null | grep 'coldstart-'); do
  gcloud redis instances delete "$i" --region "$REGION" "${P[@]}" --quiet && echo "deleted Redis $i"
done
# Serverless VPC Access connectors (only needed for Redis)
for c in $(gcloud compute networks vpc-access connectors list --region "$REGION" "${P[@]}" --format='value(name)' 2>/dev/null | grep 'coldstart-'); do
  gcloud compute networks vpc-access connectors delete "$c" --region "$REGION" "${P[@]}" --quiet && echo "deleted connector $c"
done

if [ "${DELETE_VM:-0}" = "1" ]; then
  gcloud compute instances delete "$VM" --zone "$ZONE" "${P[@]}" --quiet && echo "deleted VM $VM"
fi
if [ "${KEEP_IMAGES:-1}" = "0" ]; then
  gcloud artifacts repositories delete coldstart-lab --location "$REGION" "${P[@]}" --quiet && echo "deleted image repository"
fi

echo
echo "Left in the project (should be empty or only what you meant to keep):"
gcloud run services list --region "$REGION" "${P[@]}" --format='value(metadata.name)' 2>/dev/null
gcloud sql instances list "${P[@]}" --format='value(name)' 2>/dev/null
gcloud compute instances list "${P[@]}" --format='value(name,status)' 2>/dev/null
echo "(The billing_export dataset is kept on purpose: it costs almost nothing and holds the cost data.)"