#!/usr/bin/env bash
# Deploy HouseOps Phase 1 to Google Cloud Run
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${GCP_REGION:-me-central1}"
SERVICE_NAME="${SERVICE_NAME:-houseops}"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: Set GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT" >&2
  exit 1
fi

echo "==> Project: ${PROJECT_ID}"
echo "==> Region:  ${REGION}"
echo "==> Service: ${SERVICE_NAME}"

gcloud config set project "${PROJECT_ID}"

echo "==> Building container image..."
gcloud builds submit --tag "${IMAGE}" .

echo "==> Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --port 8080 \
  --min-instances 1 \
  --max-instances 10 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION}" \
  ${SERVICE_URL:+--set-env-vars "SERVICE_URL=${SERVICE_URL}"} \
  ${GCS_BUCKET:+--set-env-vars "GCS_BUCKET=${GCS_BUCKET}"} \
  ${INBOUND_QUEUE:+--set-env-vars "INBOUND_QUEUE=${INBOUND_QUEUE}"} \
  ${TASKS_SERVICE_ACCOUNT:+--set-env-vars "TASKS_SERVICE_ACCOUNT=${TASKS_SERVICE_ACCOUNT}"} \
  ${WHATSAPP_TOKEN:+--set-env-vars "WHATSAPP_TOKEN=${WHATSAPP_TOKEN}"} \
  ${WHATSAPP_PHONE_NUMBER_ID:+--set-env-vars "WHATSAPP_PHONE_NUMBER_ID=${WHATSAPP_PHONE_NUMBER_ID}"} \
  ${WHATSAPP_VERIFY_TOKEN:+--set-env-vars "WHATSAPP_VERIFY_TOKEN=${WHATSAPP_VERIFY_TOKEN}"} \
  ${WHATSAPP_APP_SECRET:+--set-env-vars "WHATSAPP_APP_SECRET=${WHATSAPP_APP_SECRET}"}

echo "==> Done. Service URL:"
gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)'
