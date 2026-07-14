#!/usr/bin/env bash
# ==============================================================================
# Step 4: Cost Safeguard — Teardown or Scale Down AI Hypercomputer Nodes
# ==============================================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-hypercomputer-a3-cluster}"
REGION="${REGION:-us-central1}"
NODE_POOL_NAME="${NODE_POOL_NAME:-g2-l4-pool-8g}"

echo "========================================================================"
echo "[*] Step 4: Cost Protection & Resource Teardown Options"
echo "========================================================================"
echo "Choose teardown behavior:"
echo "  [1] Scale expensive GPU Node Pool directly down to ZERO (Keep cluster & control plane intact)"
echo "  [2] Delete the entire GKE Cluster completely and purge related compute billing"
read -r -p "Enter your choice [1 or 2, default is 1]: " TEARDOWN_OPTION
TEARDOWN_OPTION="${TEARDOWN_OPTION:-1}"

if [[ "${TEARDOWN_OPTION}" == "2" ]]; then
    echo " -> Initiating permanent cluster deletion: ${CLUSTER_NAME} in ${REGION}..."
    gcloud container clusters delete "${CLUSTER_NAME}" \
        --location="${REGION}" \
        --quiet
    echo "[+] Cluster complete teardown completed successfully. Billing stopped."
else
    echo " -> Scaling down A3 GPU node pool ('${NODE_POOL_NAME}') to 0 nodes..."
    gcloud container node-pools resize "${NODE_POOL_NAME}" \
        --cluster="${CLUSTER_NAME}" \
        --location="${REGION}" \
        --num-nodes=0 \
        --quiet
    echo "[+] High-performance node pool scaled down to zero nodes! Control plane active for future jobs."
fi
