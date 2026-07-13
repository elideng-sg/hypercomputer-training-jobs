#!/usr/bin/env bash
# ==============================================================================
# Step 3: Package Benchmark Code into ConfigMap & Execute Verification Job
# ==============================================================================
set -euo pipefail

JOB_MANIFEST="configs/a3_a4_verification_job.yaml"
SOURCE_FILE="src/train_benchmark_fp8.py"

echo "========================================================================"
echo "[*] Step 3.1: Packaging benchmark python source into GKE ConfigMap..."
echo "========================================================================"
# Clean up older ConfigMap or previous job execution runs if present
kubectl delete configmap verification-source-map --ignore-not-found=true
kubectl delete -f "${JOB_MANIFEST}" --ignore-not-found=true

kubectl create configmap verification-source-map --from-file=train_benchmark_fp8.py="${SOURCE_FILE}"
echo "[+] ConfigMap 'verification-source-map' generated."

echo ""
echo "========================================================================"
echo "[*] Step 3.2: Submitting 8x GPU verification training job..."
echo "========================================================================"
kubectl apply -f "${JOB_MANIFEST}"

echo ""
echo "========================================================================"
echo "[*] Step 3.3: Waiting for container pod allocation on A3 node..."
echo "========================================================================"
POD_NAME=""
while [[ -z "${POD_NAME}" ]]; do
    POD_NAME=$(kubectl get pods -l app=gpu-nccl-test -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "${POD_NAME}" ]]; then
        echo " -> Waiting for job controller pod scheduling..."
        sleep 3
    fi
done

echo "[+] Target verification Pod identified: ${POD_NAME}"
echo " -> Tailing diagnostic output (may pause briefly while pulling NVIDIA container)..."

kubectl wait --for=condition=Ready pod/"${POD_NAME}" --timeout=300s 2>/dev/null || echo "Note: Streaming container logs as init progresses..."
kubectl logs -f pod/"${POD_NAME}" --container=verify-ddp-allreduce | tee logs/job_runtime_diagnostics.log

echo ""
echo "========================================================================"
echo "[*] Step 3.4: Extracting benchmark JSON report to local logs directory..."
echo "========================================================================"
# If pod finished successfully, copy the json metrics out
mkdir -p logs
kubectl cp "${POD_NAME}:/workspace/logs/verification_benchmark_results.json" "logs/verification_benchmark_results.json" 2>/dev/null || \
    echo "[!] Note: Check log output above for detailed latency execution metrics."

echo ""
echo "[+] Step 3 completed! Job run finished and metrics archived inside logs/ directory."
