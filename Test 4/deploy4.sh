#!/usr/bin/env bash
# Test 4: how does the size of the application change cold starts, teardown and cost?
# Builds three images from one code base (cloudbuild4.yaml) and deploys 18 scale-to-zero
# services = 3 sizes x 6 idle tiers:
#   coldstart-t4-{s,m,l}-{05,14,15,16,18,20}
#   S = the app of Tests 1-3 / the pilot, M = 1M rows + scikit-learn + small model,
#   L = M + large model (see main.py and Test4_report.md).
# Everything else identical and as in the pilot: 1 vCPU, 2 GiB, concurrency 80, min-instances 0,
# max-instances 1 (pilot decision, case C), no CPU boost, request-based billing.
# Labels (experiment=test4, size, idle_min) split the cost in the BigQuery billing export.
# Writes services4.csv (size,idle_min,offset_s,name,url). The offsets are STAGGER_S apart, in
# the order tier by tier and S, M, L within a tier, so neighbouring starts differ in size.
#
#   export PROJECT_ID=cold-start-lab
#   export REGION=europe-central2
#   bash "Test 4/deploy4.sh"              (from anywhere; paths are resolved from this file)
#   SKIP_BUILD=1 bash "Test 4/deploy4.sh" (images already built: only deploy)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
SERVICES="$HERE/services4.csv"

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-central2}"
REPO="coldstart-lab"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/shop-api"
CPU="1"
MEMORY="2Gi"
CONCURRENCY="80"
SIZES="${SIZES:-s m l}"
TIERS="${TIERS:-5 14 15 16 18 20}"
STAGGER_S="${STAGGER_S:-30}"
MODEL_MB_M="${MODEL_MB_M:-20}"
MODEL_MB_L="${MODEL_MB_L:-500}"

gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  logging.googleapis.com --project "$PROJECT_ID"
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" \
  --project "$PROJECT_ID" 2>/dev/null || true

if [ "${SKIP_BUILD:-0}" != "1" ]; then
  BUILD="$(mktemp -d)"
  trap 'rm -rf "$BUILD"' EXIT
  cp "$ROOT/app/data.py" "$ROOT/app/requirements.txt" "$BUILD/"
  cp "$HERE/main.py" "$HERE/Dockerfile" "$HERE/requirements-ml.txt" "$HERE/make_model.py" "$BUILD/"
  sed -i 's/\r$//' "$BUILD"/*
  echo ">> Building ${IMAGE}:v4-{s,m,l} (model M ${MODEL_MB_M} MB, L ${MODEL_MB_L} MB)"
  gcloud builds submit "$BUILD" --config "$HERE/cloudbuild4.yaml" --project "$PROJECT_ID" \
    --substitutions "_IMAGE=${IMAGE},_MODEL_MB_M=${MODEL_MB_M},_MODEL_MB_L=${MODEL_MB_L}"
fi

echo "size,idle_min,offset_s,name,url" > "$SERVICES"
i=0
for t in $TIERS; do
  for s in $SIZES; do
    tt="$(printf '%02d' "$t")"
    name="coldstart-t4-${s}-${tt}"
    echo ">> Deploying $name (size $s, idle tier $t min)"
    gcloud run deploy "$name" \
      --image "${IMAGE}:v4-${s}" --region "$REGION" --project "$PROJECT_ID" \
      --allow-unauthenticated --port 8080 \
      --cpu "$CPU" --memory "$MEMORY" --concurrency "$CONCURRENCY" \
      --min-instances 0 --max-instances 1 --no-cpu-boost \
      --set-env-vars "VARIANT=scale-zero" \
      --labels "experiment=test4,size=${s},idle_min=${tt}" --quiet
    url="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
    echo "${s},${t},$((i * STAGGER_S)),${name},${url}" >> "$SERVICES"
    i=$((i + 1))
  done
done

echo
echo ">> Config check (min empty/0, max 1, cpu 1, memory 2Gi, boost off, image tag):"
tail -n +2 "$SERVICES" | cut -d, -f4 | while read -r name; do
  gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(metadata.name,spec.template.metadata.annotations."autoscaling.knative.dev/minScale",spec.template.metadata.annotations."autoscaling.knative.dev/maxScale",spec.template.spec.containers[0].resources.limits.cpu,spec.template.spec.containers[0].resources.limits.memory,spec.template.metadata.annotations."run.googleapis.com/startup-cpu-boost",spec.template.spec.containers[0].image.basename())'
done
echo
cat "$SERVICES"
echo
echo "Written: $SERVICES"
echo "Next: cd \"$HERE\" && python3 measure4.py --services services4.csv --dry-run   (checks the billing export too)"
echo "Do NOT open these URLs in a browser or add uptime checks: every extra request resets the idle"
echo "timer. (The deploy itself starts one instance per service - the pilot billed 1 request per"
echo "service on deploy day - so deploy at least an hour before the run.)"
