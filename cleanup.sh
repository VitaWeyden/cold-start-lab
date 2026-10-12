#!/usr/bin/env bash
# Deletes the resources of the experiment. The warm (min=1) service keeps
# billing even when nobody uses it, so always run this when the measurements are done.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-west1}"

gcloud run services delete coldstart-scale-zero  --region "$REGION" --project "$PROJECT_ID" --quiet || true
gcloud run services delete coldstart-always-warm --region "$REGION" --project "$PROJECT_ID" --quiet || true
# Test 3 boundary services (created by deploy3.sh)
for t in 15 16 17 18 19 20; do
  gcloud run services delete "coldstart-idle-$t" --region "$REGION" --project "$PROJECT_ID" --quiet || true
done
gcloud artifacts repositories delete coldstart-lab --location "$REGION" --project "$PROJECT_ID" --quiet || true
echo "Deleted."
