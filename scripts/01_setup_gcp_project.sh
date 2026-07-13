#!/usr/bin/env bash
# ==============================================================================
# Step 1: Project Setup, Service API Activation & Compute Quota Check
# ==============================================================================
set -euo pipefail

# --- CONFIGURATION VARIABLES ---
# Specify your target GCP Project ID below before launching, or pass as $1
PROJECT_ID="${1:-${TARGET_PROJECT_ID:-}}"

if [[ -z "${PROJECT_ID}" ]]; then
    echo "[!] Error: No GCP Project ID specified."
    echo "Usage: ./scripts/01_setup_gcp_project.sh <PROJECT_ID>"
    exit 1
fi

TARGET_REGION="us-central1"
TARGET_ZONE="us-central1-a"

echo "========================================================================"
echo "[*] Step 1.1: Configuring active gcloud profile against Project: ${PROJECT_ID}"
echo "========================================================================"
gcloud config set project "${PROJECT_ID}"
gcloud config set compute/region "${TARGET_REGION}"
gcloud config set compute/zone "${TARGET_ZONE}"

echo ""
echo "========================================================================"
echo "[*] Step 1.2: Enabling essential AI Hypercomputer GCP APIs..."
echo "========================================================================"
gcloud services enable \
    compute.googleapis.com \
    container.googleapis.com \
    tpu.googleapis.com \
    storage-component.googleapis.com \
    cloudresourcemanager.googleapis.com

echo ""
echo "========================================================================"
echo "[*] Step 1.3: Checking GPU quotas in region ${TARGET_REGION}..."
echo "========================================================================"
echo "-> Checking NVIDIA H100 GPU quota for A3 instances (`a3-highgpu-8g` requires >= 8):"
gcloud compute regions describe "${TARGET_REGION}" \
    --format="table(quotas.metric, quotas.limit, quotas.usage)" | \
    grep -E "NVIDIA_H100_GPUS|NVIDIA_H200_GPUS|NVIDIA_L4_GPUS" || echo "Note: Check custom quota view in Google Cloud Console if metric names vary by billing tier."

echo ""
echo "[+] Step 1 completed successfully! GCP project ${PROJECT_ID} initialized and ready for cluster deployment."
