#!/usr/bin/env bash
# Test 4 addition: the always-warm side (min-instances=1) for the three sizes.
# Decided on 2026-11-20 after the main Test 4 run (which had only scale-to-zero services);
# see Test4_report.md section 10. Deploys 3 services from the images built for Test 4
# (no build):  coldstart-t4w-{s,m,l}-16
# Same as the Test 4 services except min-instances 1: 1 vCPU, 2 GiB, concurrency 80,
# max-instances 1, no CPU boost, request-based billing. Labels: experiment=test4w, size, idle_min.
# Writes services4w.csv (size,idle_min,offset_s,name,url) for measure4.py.
#
# !! A min-instance bills for every second it exists (~EUR 0.033/h per service at 1 vCPU + 2 GiB),
# !! with or without requests. Deploy about an hour before the start and delete right after:
#      DELETE=1 bash deploy4w.sh
#
#   export PROJECT_ID=cold-start-lab REGION=europe-central2
#   bash "Test 4/deploy4w.sh"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SERVICES="$HERE/services4w.csv"
PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-central2}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/coldstart-lab/shop-api"
SIZES="${SIZES:-s m l}"
TIER="${TIER:-16}"
STAGGER_S="${STAGGER_S:-30}"

if [ "${DELETE:-0}" = "1" ]; then
  for s in $SIZES; do
    gcloud run services delete "coldstart-t4w-${s}-${TIER}" --region "$REGION" --project "$PROJECT_ID" --quiet \
      && echo "deleted coldstart-t4w-${s}-${TIER}"
  done
  echo "Left (coldstart-t4w-*):"
  gcloud run services list --region "$REGION" --project "$PROJECT_ID" --format='value(metadata.name)' | grep '^coldstart-t4w-' || echo "  none"
  exit 0
fi

echo "size,idle_min,offset_s,name,url" > "$SERVICES"
i=0
for s in $SIZES; do
  name="coldstart-t4w-${s}-${TIER}"
  echo ">> Deploying $name (size $s, ALWAYS-WARM: min-instances 1)"
  gcloud run deploy "$name" \
    --image "${IMAGE}:v4-${s}" --region "$REGION" --project "$PROJECT_ID" \
    --allow-unauthenticated --port 8080 \
    --cpu 1 --memory 2Gi --concurrency 80 \
    --min-instances 1 --max-instances 1 --no-cpu-boost \
    --set-env-vars "VARIANT=always-warm" \
    --labels "experiment=test4w,size=${s},idle_min=${TIER}" --quiet
  url="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
  echo "${s},${TIER},$((i * STAGGER_S)),${name},${url}" >> "$SERVICES"
  i=$((i + 1))
done

echo
echo ">> Config check (min 1, max 1, cpu 1, memory 2Gi, boost off, image tag):"
for s in $SIZES; do
  gcloud run services describe "coldstart-t4w-${s}-${TIER}" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(metadata.name,spec.template.metadata.annotations."autoscaling.knative.dev/minScale",spec.template.metadata.annotations."autoscaling.knative.dev/maxScale",spec.template.spec.containers[0].resources.limits.cpu,spec.template.spec.containers[0].resources.limits.memory,spec.template.metadata.annotations."run.googleapis.com/startup-cpu-boost",spec.template.spec.containers[0].image.basename())'
done
echo
cat "$SERVICES"
echo "Written: $SERVICES   (deployed at $(date -u +%FT%TZ) - note it: the min-instance bill starts now)"
echo "Next (on the VM): python3 measure4.py --services services4w.csv --dry-run"
echo "After the run, as soon as the results are downloaded:  DELETE=1 bash deploy4w.sh"
