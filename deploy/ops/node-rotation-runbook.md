# Node Rotation Runbook

## Overview
DWS Flex-Start A3 nodes have a 7-day lifetime. This runbook documents the **single-replica reschedule** continuity path for node rotation — the model cache PVC (ReadWriteOnce) reattaches in the same zone with a brief serving gap during replacement node provisioning.

## Procedure (Single-Replica Reschedule)

**Supported continuity path:** When the node expires or is lost, the Deployment recreates the pod and the ReadWriteOnce PD **reattaches in the same zone** with the model cache intact — a **brief serving gap** while the replacement node provisions (DWS-queued, capacity-permitting). This is **continuity, not zero-downtime.**

### Step 1: Pre-provision (Days 5-6)
Before the current node expires (~day 7), submit a new DWS request for a replacement A3 node in the same zone to minimize the gap.

### Step 2: Let old node expire or cordon/drain
Allow the old DWS node to reach its 7-day expiry and be reclaimed. Or, if you need to force rotation early:
```bash
kubectl cordon <old-node>
kubectl drain <old-node> --ignore-daemonsets --delete-emptydir-data
```

**Important:** The PodDisruptionBudget (`minAvailable: 1`) on a single-replica Deployment will **block** `kubectl drain` until you temporarily scale up or delete the PDB. The documented order (let it expire naturally, or scale to 0 before drain) avoids this.

### Step 3: Verify the pod reschedules
Once the replacement node is ready, the pending pod will bind and the service resumes:
```bash
kubectl -n inference get pods -l app=qwen3-vllm
kubectl -n inference describe service qwen3-vllm
```

The `hf-cache` PVC (ReadWriteOnce) reattaches to the new node automatically.

## Notes
- **PVCs (Persistent Disk):** Reattach automatically in the same zone (ReadWriteOnce allows one node at a time).
- **Kueue auto-reprovision:** A pending pod triggers Kueue to auto-reprovision a node if capacity is available.
- **Capacity caveat:** If no A3 capacity is available, the gap window extends until capacity is granted.

## Zero-Downtime Overlap (Not Currently Configured)

**True zero-downtime overlap** (2 replicas across two nodes during rotation) is **NOT possible with the current setup** and would require:

1. **ReadWriteMany model cache** (e.g. Filestore) instead of the ReadWriteOnce PD, since a PD can't attach to two nodes simultaneously.
2. **Pod anti-affinity / topology spread constraints** in the Deployment to force the replicas onto separate nodes (currently not configured).

These are marked as future enhancements, not currently deployed.
