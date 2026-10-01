#!/usr/bin/env bash
# Deploys two Cloud Run services from the SAME image with identical resources.
# The only difference: --min-instances 0 (scale-to-zero) vs 1 (warm instance).
#
# Usage:
#   export PROJECT_ID=your-project-id
#   export REGION=europe-west1      # optional
#   ./deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set it first: export PROJECT_ID=...}"
REGION="${REGION:-europe-west1}"
REPO="coldstart-lab"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/shop-api:v1"

# Shared resource settings (must be identical for both services!)
CPU="1"
MEMORY="1Gi"
CONCURRENCY="80"
MAX_INSTANCES="1"

echo ">> Enabling APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com --project "$PROJECT_ID"

echo ">> Artifact Registry repository (if it does not exist yet)"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" --project "$PROJECT_ID" 2>/dev/null || true

echo ">> Building and pushing the image"
gcloud builds submit app --tag "$IMAGE" --project "$PROJECT_ID"

deploy() {
  local name="$1" min="$2" variant="$3"
  echo ">> Deploying: $name (min-instances=$min)"
  gcloud run deploy "$name" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT_ID" \
    --allow-unauthenticated \
    --port 8080 \
    --cpu "$CPU" --memory "$MEMORY" \
    --concurrency "$CONCURRENCY" \
    --min-instances "$min" --max-instances "$MAX_INSTANCES" \
    --no-cpu-boost \
    --set-env-vars "VARIANT=${variant}"
}

deploy "coldstart-scale-zero" 0 "scale-zero"
deploy "coldstart-always-warm" 1 "always-warm"

url() {
  gcloud run services describe "$1" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(status.url)'
}

{
  echo "SCALE_ZERO_URL=$(url coldstart-scale-zero)"
  echo "WARM_URL=$(url coldstart-always-warm)"
} | tee urls.env

echo
echo "Done. The service URLs are saved in urls.env."
echo "Check:  curl \$(grep SCALE_ZERO_URL urls.env | cut -d= -f2)/"
