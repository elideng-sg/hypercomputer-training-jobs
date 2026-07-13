#!/usr/bin/env bash
# ==============================================================================
# Step 2: Provision AI Hypercomputer Cluster & High-Performance A3/A4 Node Pool
# ==============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hypercomputer-a3-cluster}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"

# Target Machine configuration (Default: a3-highgpu-8g with 8x H100 80GB GPUs)
# Alternative values for higher tiers:
#   A3 Ultra H200: --machine-type=a3-ultragpu-8g --accelerator=type=nvidia-h200-141gb,count=8
#   A4 Blackwell:  --machine-type=a4-highgpu-8g --accelerator=type=nvidia-b200,count=8
NODE_POOL_NAME="a3-h100-pool-8g"
MACHINE_TYPE="a3-highgpu-8g"
ACCELERATOR_TYPE="nvidia-h100-80gb"
GPU_COUNT="8"
NUM_NODES="1" # Start with 1 full intact 8-GPU node for verification test

echo "========================================================================"
echo "[*] Step 2.1: Creating foundational GKE control plane: ${CLUSTER_NAME}..."
echo "========================================================================"
# Check if cluster already exists
if gcloud container clusters describe "${CLUSTER_NAME}" --location="${REGION}" >/dev/null 2>&1; then
    echo "[+] Cluster '${CLUSTER_NAME}' already exists in ${REGION}."
else
    gcloud container clusters create "${CLUSTER_NAME}" \
        --location="${REGION}" \
        --node-locations="${ZONE}" \
        --release-channel="rapid" \
        --num-nodes="1" \
        --machine-type="e2-standard-4" \
        --enable-ip-alias \
        --workload-pool=$(gcloud config get-value project).svc.id.goog
    echo "[+] Control plane created successfully."
fi

echo ""
echo "========================================================================"
echo "[*] Step 2.2: Retrieving Kubernetes cluster configuration details..."
echo "========================================================================"
gcloud container clusters get-credentials "${CLUSTER_NAME}" --location="${REGION}"

echo ""
echo "========================================================================"
echo "[*] Step 2.3: Provisioning A3 8x GPU Node Pool (${NODE_POOL_NAME})..."
echo "========================================================================"
if gcloud container node-pools describe "${NODE_POOL_NAME}" --cluster="${CLUSTER_NAME}" --location="${REGION}" >/dev/null 2>&1; then
    echo "[+] Node pool '${NODE_POOL_NAME}' already exists."
else
    # Provision intact 8-GPU A3 Hypercomputer Node with automated LTSB GPU driver installation
    # and gVNIC networking interfaces enabled for line-rate intra-node transfers.
    gcloud container node-pools create "${NODE_POOL_NAME}" \
        --cluster="${CLUSTER_NAME}" \
        --location="${REGION}" \
        --node-locations="${ZONE}" \
        --machine-type="${MACHINE_TYPE}" \
        --accelerator="type=${ACCELERATOR_TYPE},count=${GPU_COUNT}" \
        --gpu-driver-version="default" \
        --num-nodes="${NUM_NODES}" \
        --enable-gvnic \
        --disk-size="500GB" \
        --disk-type="pd-ssd" \
        --tags="ai-hypercomputer,a3-gpu-node" \
        --scopes="https://www.googleapis.com/auth/cloud-platform" \
        --labels="node.kubernetes.io/instance-type=${MACHINE_TYPE},topology.kubernetes.io/gpu-cluster=a3-h100"
        
    echo "[+] High-performance 8x GPU node pool provisioned successfully."
fi

echo ""
echo "========================================================================"
echo "[*] Step 2.4: Verifying Kubernetes node ready and GPU resource status..."
echo "========================================================================"
kubectl get nodes -l "node.kubernetes.io/instance-type=${MACHINE_TYPE}" -o wide

echo ""
echo "[+] Step 2 completed! A3 cluster ready to run distributed multi-GPU workloads."
