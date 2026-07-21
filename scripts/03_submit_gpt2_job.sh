#!/usr/bin/env bash
# ==============================================================================
# Step 3 (GPT-2): Package GPT-2 Benchmark Code & Execute Distributed Training
# ==============================================================================
set -euo pipefail

JOB_MANIFEST="configs/gpt2_training_job.yaml"
SOURCE_FILE="src/train_benchmark_gpt2.py"

# Defaults
TP_SIZE=2
PP_SIZE=2
BATCH_SIZE=4
MAX_STEPS=100
DATASET_BIN=""
INSTANCE_TYPE=""

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tp-size)
            TP_SIZE="$2"
            shift 2
            ;;
        --pp-size)
            PP_SIZE="$2"
            shift 2
            ;;
        --batch-size)
            BATCH_SIZE="$2"
            shift 2
            ;;
        --max-steps)
            MAX_STEPS="$2"
            shift 2
            ;;
        --dataset-bin)
            DATASET_BIN="$2"
            shift 2
            ;;
        --instance-type)
            INSTANCE_TYPE="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--tp-size <size>] [--pp-size <size>] [--batch-size <size>] [--max-steps <steps>] [--dataset-bin <bin_path>] [--instance-type <type>]"
            exit 1
            ;;
    esac
done

# Auto-detect instance type if not explicitly overridden by user
if [[ -z "${INSTANCE_TYPE}" ]]; then
    echo "[*] Auto-detecting GKE GPU node instance type..."
    DETECTED_TYPE=$(kubectl get nodes -l cloud.google.com/gke-gpu=true -o jsonpath='{.items[0].metadata.labels.node\.kubernetes\.io/instance-type}' 2>/dev/null || true)
    if [[ -z "${DETECTED_TYPE}" ]]; then
        echo " -> No active GPU nodes running. Defaulting to g2-standard-96 (autoscaler target)."
        INSTANCE_TYPE="g2-standard-96"
    else
        echo "[+] Auto-detected instance type: ${DETECTED_TYPE}"
        INSTANCE_TYPE="${DETECTED_TYPE}"
    fi
else
    echo "[+] Using user-configured instance type: ${INSTANCE_TYPE}"
fi

echo "========================================================================"
echo "[*] Step 3.1: Packaging GPT-2 benchmark python source into GKE ConfigMap..."
echo "========================================================================"
# Clean up older Job and ConfigMap
kubectl delete job gcp-ai-hypercomputer-gpt2-training --ignore-not-found=true
kubectl delete configmap gpt2-source-map --ignore-not-found=true

kubectl create configmap gpt2-source-map --from-file=train_benchmark_gpt2.py="${SOURCE_FILE}"
echo "[+] ConfigMap 'gpt2-source-map' generated."

echo ""
echo "========================================================================"
echo "[*] Step 3.2: Submitting 8x GPU GPT-2 training job..."
echo "    Parameters: TP=${TP_SIZE}, PP=${PP_SIZE}, BS=${BATCH_SIZE}, Steps=${MAX_STEPS}"
echo "    Dataset: ${DATASET_BIN}"
echo "    Instance Type: ${INSTANCE_TYPE}"
echo "========================================================================"

# Substitute placeholders on-the-fly and submit to Kubernetes
sed -e "s|_TP_SIZE_|${TP_SIZE}|g" \
    -e "s|_PP_SIZE_|${PP_SIZE}|g" \
    -e "s|_BATCH_SIZE_|${BATCH_SIZE}|g" \
    -e "s|_MAX_STEPS_|${MAX_STEPS}|g" \
    -e "s|_DATASET_BIN_|${DATASET_BIN}|g" \
    -e "s|_INSTANCE_TYPE_|${INSTANCE_TYPE}|g" \
    "${JOB_MANIFEST}" | kubectl apply -f -

echo ""
echo "========================================================================"
echo "[*] Step 3.3: Waiting for container pod allocation on A3 node..."
echo "========================================================================"
POD_NAME=""
while [[ -z "${POD_NAME}" ]]; do
    POD_NAME=$(kubectl get pods -l app=gpt2-training -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "${POD_NAME}" ]]; then
        echo " -> Waiting for job controller pod scheduling..."
        sleep 3
    fi
done

echo "[+] Target GPT-2 training Pod identified: ${POD_NAME}"
echo " -> Tailing diagnostic output (may pause briefly while pulling NVIDIA container)..."

kubectl wait --for=condition=Ready pod/"${POD_NAME}" --timeout=300s 2>/dev/null || echo "Note: Streaming container logs as init progresses..."
kubectl logs -f pod/"${POD_NAME}" --container=gpt2-train | tee logs/gpt2_job_runtime_diagnostics.log

echo ""
echo "[+] Step 3 (GPT-2) completed! Job run finished and metrics archived inside logs/ directory."
