#!/usr/bin/env bash
# ==============================================================================
# Step 2: Provision AI Hypercomputer Cluster & High-Performance A3/A4 Node Pool
# ==============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hypercomputer-a3-cluster}"
REGION="${REGION:-us-east4}"
ZONE="${ZONE:-us-east4-a}"
NODE_ZONES="${NODE_ZONES:-us-east4-a,us-east4-b,us-east4-c}"

# Target Machine configuration (Default: a3-highgpu-8g with 8x H100 80GB GPUs)
# Alternative values for higher tiers:
#   A3 Ultra H200: --machine-type=a3-ultragpu-8g --accelerator=type=nvidia-h200-141gb,count=8
#   A4 Blackwell:  --machine-type=a4-highgpu-8g --accelerator=type=nvidia-b200,count=8
NODE_POOL_NAME="a3-h100-pool-8g"
MACHINE_TYPE="a3-highgpu-8g"
ACCELERATOR_TYPE="nvidia-h100-80gb"
GPU_COUNT="8"
NUM_NODES="0" # Start at 0 nodes for Dynamic Workload Scheduler (DWS) Queued Provisioning
MIN_NODES="0"
MAX_NODES="4" # Max autoscale target across zones across us-east4-a/b/c

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
        --release-channel="None" \
        --cluster-version="1.33.13-gke.1101000" \
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
    # Opt cluster out of release channel so manual COS 121 version pinning & disabling auto-upgrade are permitted
    echo "[*] Unenrolling control plane from automatic release channels for COS 121 kernel compatibility..."
    gcloud container clusters update "${CLUSTER_NAME}" --location="${REGION}" --release-channel="None" >/dev/null 2>&1 || true

    # Provision intact 8-GPU A3 Hypercomputer Node Pool using Dynamic Workload Scheduler (DWS)
    # Queued Provisioning (--enable-queued-provisioning) + zero-to-N autoscaling across available zones
    # to eliminate synchronous [GCE_STOCKOUT] initialization timeouts.
    gcloud container node-pools create "${NODE_POOL_NAME}" \
        --cluster="${CLUSTER_NAME}" \
        --location="${REGION}" \
        --node-locations="${NODE_ZONES}" \
        --machine-type="${MACHINE_TYPE}" \
        --node-version="1.33.13-gke.1101000" \
        --no-enable-autoupgrade \
        --accelerator="type=${ACCELERATOR_TYPE},count=${GPU_COUNT},gpu-driver-version=default" \
        --enable-queued-provisioning \
        --enable-autoscaling \
        --min-nodes="${MIN_NODES}" \
        --max-nodes="${MAX_NODES}" \
        --location-policy="ANY" \
        --num-nodes="${NUM_NODES}" \
        --enable-gvnic \
        --disk-size="500GB" \
        --disk-type="pd-ssd" \
        --tags="ai-hypercomputer,a3-gpu-node" \
        --scopes="https://www.googleapis.com/auth/cloud-platform" \
        --node-labels="gpu-cluster=a3-h100,cloud.google.com/gke-queued=true" \
        --labels="machine-type=${MACHINE_TYPE},gpu-cluster=a3-h100,dws=queued-provisioning"
        
    echo "[+] High-performance 8x GPU node pool configured successfully with DWS Queued Provisioning."
fi

echo ""
echo "========================================================================"
echo "[*] Step 2.4: Verifying Kubernetes node ready and GPU resource status..."
echo "========================================================================"
echo "-> Checking underlying GPU instances directly via gcloud:"
gcloud compute instances list --filter="name~gke-hypercomputer-a3" --format="table(name,zone,machineType,status)"

echo ""
echo "-> Checking Kubernetes Node API (skips if local kubectl binary is blocked by Santa):"
if ! kubectl get nodes -l "node.kubernetes.io/instance-type=${MACHINE_TYPE}" -o wide 2>/dev/null; then
    echo "[!] Note: local kubectl execution was interrupted or blocked by security policies."
    echo "    Use Cloud Shell (https://shell.cloud.google.com/?project=$(gcloud config get-value project)) or gcloud UI to interact directly via Kubernetes API."
fi

echo ""
echo "[+] Step 2 completed! A3 cluster infrastructure is configured and registered."
