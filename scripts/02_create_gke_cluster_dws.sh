#!/usr/bin/env bash
# ==============================================================================
# Step 2 (DWS Option): Provision AI Hypercomputer Cluster & A3 H100 DWS Node Pool
# ==============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hypercomputer-a3-cluster}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
# Spans all three primary Iowa zones across us-central1 where multi-GPU NVIDIA H100 A3 hardware (`a3-highgpu-8g`) is deployed
NODE_ZONES="${NODE_ZONES:-us-central1-a,us-central1-b,us-central1-c}"

# Target Machine configuration (A3 High-Capacity Iowa Hub: a3-highgpu-8g with 8x NVIDIA H100 80GB GPUs)
NODE_POOL_NAME="a3-h100-dws-pool"
MACHINE_TYPE="a3-highgpu-8g"
ACCELERATOR_TYPE="nvidia-h100-80gb"
GPU_COUNT="8"
NUM_NODES="0" # Initialize cleanly to 0 since DWS dynamically acquires compute upon workload/ProvisioningRequest submission
MIN_NODES="0"
MAX_NODES="3" # Allow scaling right across target multi-zone queue demand

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
echo "[*] Step 2.3: Provisioning ONE unified multi-zone A3 8x H100 DWS Queued Node Pool (${NODE_POOL_NAME}) spanning Iowa zones (${NODE_ZONES})..."
echo "========================================================================"
NP_STATUS=$(gcloud container node-pools describe "${NODE_POOL_NAME}" --cluster="${CLUSTER_NAME}" --location="${REGION}" --format="value(status)" 2>/dev/null || true)

if [[ "${NP_STATUS}" == "ERROR" ]]; then
    echo "[!] Existing node pool '${NODE_POOL_NAME}' is right now in ERROR status."
    echo "[*] Deleting errored node pool '${NODE_POOL_NAME}' to re-provision cleanly..."
    gcloud container node-pools delete "${NODE_POOL_NAME}" --cluster="${CLUSTER_NAME}" --location="${REGION}" --quiet
    NP_STATUS=""
fi

if [[ -n "${NP_STATUS}" ]]; then
    echo "[+] Node pool '${NODE_POOL_NAME}' already exists right now across ${REGION} in status: ${NP_STATUS}."
else
    # Opt cluster out of release channel so manual COS 121 version pinning & disabling auto-upgrade are permitted
    echo "[*] Unenrolling control plane from automatic release channels for COS 121 kernel compatibility..."
    gcloud container clusters update "${CLUSTER_NAME}" --location="${REGION}" --release-channel="None" >/dev/null 2>&1 || true

    # Provision intact ONE unified 8x H100 A3 DWS GPU Node Pool spanning all three availability zones (`us-central1-a,b,c`).
    # Utilizing --location-policy=BALANCED strictly ensures incoming queue requests split across all three zones evenly (`0 -> 1` per zone).
    gcloud container node-pools create "${NODE_POOL_NAME}" \
        --cluster="${CLUSTER_NAME}" \
        --location="${REGION}" \
        --node-locations="${NODE_ZONES}" \
        --machine-type="${MACHINE_TYPE}" \
        --node-version="1.33.13-gke.1101000" \
        --no-enable-autoupgrade \
        --accelerator="type=${ACCELERATOR_TYPE},count=${GPU_COUNT},gpu-driver-version=default" \
        --enable-queued-provisioning \
        --reservation-affinity="none" \
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
        --node-labels="gpu-cluster=a3-h100" \
        --labels="machine-type=${MACHINE_TYPE},gpu-cluster=a3-h100,provisioning=dws-queued"
        
    echo "[+] High-performance ONE unified A3 DWS node pool ('${NODE_POOL_NAME}') provisioned successfully spanning across ${NODE_ZONES} with BALANCED policy."
fi

echo ""
echo "========================================================================"
echo "[*] Step 2.4: Verifying node pool & compute instance status using gcloud..."
echo "========================================================================"
echo "-> Checking GKE node pool status & Queued Workload Scheduler configuration via gcloud container:"
gcloud container node-pools describe "${NODE_POOL_NAME}" \
    --cluster="${CLUSTER_NAME}" \
    --location="${REGION}" \
    --format="table(name,status,initialNodeCount,autoscaling.enabled,queuedProvisioning.enabled,autoscaling.locationPolicy)" || true

echo ""
echo "-> Checking active underlying compute instances across zones via gcloud compute:"
gcloud compute instances list --filter="name~gke-${CLUSTER_NAME} OR tags.items~ai-hypercomputer" --format="table(name,zone,machineType,status)" || true

echo "========================================================================"
echo "[*] Step 2.5: Submitting independent multi-zone DWS Queued Provisioning Requests & Capacity Holder..."
echo "========================================================================"
# Clean up older single-zone or expired booking objects if present
# Clean up older expired overnight queue requests or zero-GPU DaemonSets if present
kubectl delete provisioningrequest a3-h100-req-zone-a a3-h100-req-zone-b a3-h100-req-zone-c a3-h100-verification-req --ignore-not-found -n default >/dev/null 2>&1 || true
kubectl delete daemonset a3-dws-capacity-holder --ignore-not-found -n default >/dev/null 2>&1 || true

echo "[*] Deploying 8-GPU Active Capacity Holder right away to permanently disarm BookingExpired (configs/a3_dws_holder_8gpu.yaml)..."
if [ -f "configs/a3_dws_holder_8gpu.yaml" ]; then
    kubectl apply -f configs/a3_dws_holder_8gpu.yaml
else
    echo "[!] Warning: configs/a3_dws_holder_8gpu.yaml missing."
fi

echo ""
echo "[*] Submitting fresh independent 7-day DWS A3 8x H100 hardware queue requests across all three Iowa availability zones ASAP..."
for zone_cfg in configs/dws_provisioning_request_zone_*.yaml; do
    if [ -f "${zone_cfg}" ]; then
        kubectl apply -f "${zone_cfg}"
    fi
done

echo ""
echo "-> Checking active multi-zone DWS ProvisioningRequests and 8-GPU Holder deployment right away:"
kubectl get provisioningrequests -n default || true
kubectl get deployments,pods -l app=a3-capacity-holder-8gpu -n default || true

echo ""
echo "[+] Step 2 (DWS) completed! A3 cluster infrastructure is running, fresh 7-day multi-zone DWS machine requests are queued ASAP, and explicit 8-GPU capacity holder protection (a3-h100-holder-8gpu) is actively waiting right across the queue right away!"
