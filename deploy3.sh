#!/usr/bin/env bash
# Test 3: deploys one extra scale-to-zero Cloud Run service per idle tier (15..20 min).
# Image, CPU, memory and concurrency are COPIED from the existing coldstart-scale-zero
# service, so the new services are guaranteed identical to the ones measured in Test 1/2.
# Each service is labelled (experiment=boundary, idle_min=N) so the BigQuery billing
# export can attribute cost per service.
#
#   export PROJECT_ID=your-project-id
#   export REGION=europe-central2
#   ./deploy3.sh            # writes services3.csv (idle_min,name,url)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-central2}"
SOURCE="${SOURCE:-coldstart-scale-zero}"
TIERS="${TIERS:-15 16 17 18 19 20}"

desc() { gcloud run services describe "$SOURCE" --region "$REGION" --project "$PROJECT_ID" --format="value($1)"; }

IMAGE="$(desc 'spec.template.spec.containers[0].image')"
CPU="$(desc 'spec.template.spec.containers[0].resources.limits.cpu')"
MEMORY="$(desc 'spec.template.spec.containers[0].resources.limits.memory')"
CONCURRENCY="$(desc 'spec.template.spec.containerConcurrency')"
for v in IMAGE CPU MEMORY CONCURRENCY; do
  [ -n "${!v}" ] || { echo "Could not read $v from $SOURCE - is the service deployed in $REGION?"; exit 1; }
done
case "$CPU" in *m) CPU="$(awk "BEGIN{print ${CPU%m}/1000}")" ;; esac   # 1000m -> 1
echo ">> Copying config from $SOURCE: image=$IMAGE cpu=$CPU memory=$MEMORY concurrency=$CONCURRENCY"

: > services3.csv
echo "idle_min,name,url" >> services3.csv
for t in $TIERS; do
  name="coldstart-idle-$t"
  echo ">> Deploying $name"
  gcloud run deploy "$name" \
    --image "$IMAGE" --region "$REGION" --project "$PROJECT_ID" \
    --allow-unauthenticated --port 8080 \
    --cpu "$CPU" --memory "$MEMORY" --concurrency "$CONCURRENCY" \
    --min-instances 0 --max-instances 1 --no-cpu-boost \
    --set-env-vars "VARIANT=scale-zero" \
    --labels "experiment=boundary,idle_min=$t" --quiet
  url="$(gcloud run services describe "$name" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
  echo "$t,$name,$url" >> services3.csv
done

echo
echo ">> Config check (every row must show min=0 max=1 and the same cpu/memory):"
for t in $TIERS; do
  gcloud run services describe "coldstart-idle-$t" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(metadata.name,spec.template.metadata.annotations."autoscaling.knative.dev/minScale",spec.template.metadata.annotations."autoscaling.knative.dev/maxScale",spec.template.spec.containers[0].resources.limits.cpu,spec.template.spec.containers[0].resources.limits.memory)'
done
echo
cat services3.csv
