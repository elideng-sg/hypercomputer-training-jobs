#!/usr/bin/env bash
set -euo pipefail
kubectl -n inference delete deploy qwen3-vllm --ignore-not-found
kubectl -n default scale deploy/a3-holder-zone-a --replicas=1
echo "[+] services torn down; holder re-armed (house rule)"
