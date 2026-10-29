#!/usr/bin/env bash
# Test 4 pilot: does max-instances=1 cause the HTTP 429s seen in Test 3?
# Builds the v4 image (same app + instance/request logging) and deploys 6 scale-to-zero services:
#   coldstart-p4-m1-{a,b,c}   max-instances=1   (the Test 1-3 setting)
#   coldstart-p4-m3-{a,b,c}   max-instances=3
# Everything else identical: 1 vCPU, 2 GiB (the Test 4 memory), concurrency 80, min-instances 0,
# no CPU boost, request-based billing. Labels let the BigQuery billing export split the cost.
# Writes services4p.csv (arm,replica,idle_min,offset_s,name,url); the offsets interleave the two
# arms 20 s apart, so no two cold starts fall in the same second.
#
#   export PROJECT_ID=cold-start-lab
#   export REGION=europe-central2
#   bash "Test 4/deploy4p.sh"      (from anywhere; the paths are resolved from this file)
#
# The image is built from app/ with app/main.py replaced by Test 4/main.py, so app/ itself stays
# the v1 code of Tests 1-3 and the v4 app lives next to the Test 4 scripts.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
SERVICES="$HERE/services4p.csv"

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-central2}"
REPO="coldstart-lab"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/shop-api:v4"
CPU="1"
MEMORY="2Gi"
CONCURRENCY="80"
IDLE_MIN="${IDLE_MIN:-16}"
STAGGER_S="${STAGGER_S:-20}"
REPLICAS="${REPLICAS:-a b c}"

gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  logging.googleapis.com --project "$PROJECT_ID"
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" \
  --project "$PROJECT_ID" 2>/dev/null || true

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
cp -r "$ROOT/app/." "$BUILD/"
rm -rf "$BUILD/__pycache__"
cp "$HERE/main.py" "$BUILD/main.py"
sed -i 's/\r$//' "$BUILD/main.py"
grep -q "instance_identity" "$BUILD/main.py" || { echo "Test 4/main.py is not the v4 app"; exit 1; }
echo ">> Building $IMAGE from app/ + Test 4/main.py"
gcloud builds submit "$BUILD" --tag "$IMAGE" --project "$PROJECT_ID"

echo "arm,replica,idle_min,offset_s,name,url" > "$SERVICES"
i=0
for r in $REPLICAS; do
  for max in 1 3; do
    name="coldstart-p4-m${max}-${r}"
    echo ">> Deploying $name (max-instances=$max)"
    gcloud run deploy "$name" \
      --image "$IMAGE" --region "$REGION" --project "$PROJECT_ID" \
      --allow-unauthenticated --port 8080 \
      --cpu "$CPU" --memory "$MEMORY" --concurrency "$CONCURRENCY" \
      --min-instances 0 --max-instances "$max" --no-cpu-boost \
      --set-env-vars "VARIANT=scale-zero" \
      --labels "experiment=pilot4,max_inst=${max},replica=${r}" --quiet
    url="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
    echo "max${max},${r},${IDLE_MIN},$((i * STAGGER_S)),${name},${url}" >> "$SERVICES"
    i=$((i + 1))
  done
done

echo
echo ">> Config check (min must be empty/0, max 1 or 3, cpu 1, memory 2Gi, boost off):"
for r in $REPLICAS; do for max in 1 3; do
  gcloud run services describe "coldstart-p4-m${max}-${r}" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(metadata.name,spec.template.metadata.annotations."autoscaling.knative.dev/minScale",spec.template.metadata.annotations."autoscaling.knative.dev/maxScale",spec.template.spec.containers[0].resources.limits.cpu,spec.template.spec.containers[0].resources.limits.memory,spec.template.metadata.annotations."run.googleapis.com/startup-cpu-boost")'
done; done
echo
cat "$SERVICES"
echo
echo "Written: $SERVICES"
echo "Next: cd \"$HERE\" && python3 measure4.py --services services4p.csv --dry-run   (checks the billing export too)"
echo "Do NOT open these URLs in a browser or add uptime checks: every extra request resets the idle timer."