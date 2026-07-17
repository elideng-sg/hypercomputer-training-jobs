#!/usr/bin/env bash
set -euo pipefail
kubectl apply -f deploy/inference/  # PVC, deployment, service, pdb
kubectl -n default scale deploy/a3-holder-zone-a --replicas=0
echo "[+] holder released; vLLM binding the A3 node"
