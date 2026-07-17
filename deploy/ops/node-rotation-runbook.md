# Node Rotation Runbook

## Overview
DWS Flex-Start A3 nodes have a 7-day lifetime. This runbook ensures zero-downtime vLLM service continuity during node rotation using a 2-replica overlap strategy.

## Procedure

### Step 1: Pre-provision (Days 5-6)
Before the current node expires (~day 7), submit a new DWS request for a second A3 node in the same zone.

### Step 2: Scale to 2 replicas
Once the new node is ready:
```bash
kubectl -n inference scale deploy/qwen3-vllm --replicas=2
```
The PodDisruptionBudget (minAvailable: 1) ensures at least one pod stays serving during the transition. Each replica binds to a separate node.

### Step 3: Verify health
Confirm both endpoints are healthy:
```bash
kubectl -n inference get pods -l app=qwen3-vllm
kubectl -n inference describe service qwen3-vllm-service
```

### Step 4: Drain old node
```bash
kubectl cordon <old-node>
kubectl drain <old-node> --ignore-daemonsets --delete-emptydir-data
```
The PDB prevents both pods from being evicted simultaneously.

### Step 5: Let old node expire
Allow the old DWS node to reach its 7-day expiry and be reclaimed.

### Step 6: Scale back to 1
```bash
kubectl -n inference scale deploy/qwen3-vllm --replicas=1
```

## Notes
- **PVCs (Persistent Disk):** Reattach automatically in the same zone.
- **Kueue auto-reprovision:** A pending pod triggers Kueue to auto-reprovision a node if capacity is available.
- **Capacity caveat:** If no A3 capacity is available, the overlap window may be blocked. Plan provisioning accordingly.
