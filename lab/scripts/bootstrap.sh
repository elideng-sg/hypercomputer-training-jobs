#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT:?set PROJECT}"; : "${REGION:?set REGION}"
STATE_BUCKET="gs://${PROJECT}-lab-tfstate"
gcloud services enable compute.googleapis.com container.googleapis.com \
  networkmanagement.googleapis.com iam.googleapis.com --project "$PROJECT"
gsutil ls "$STATE_BUCKET" >/dev/null 2>&1 || gsutil mb -p "$PROJECT" -l "$REGION" "$STATE_BUCKET"
gsutil versioning set on "$STATE_BUCKET"
echo "[+] state bucket ready: $STATE_BUCKET"
