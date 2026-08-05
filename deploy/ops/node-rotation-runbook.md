# Node Rotation Runbook

## Overview
Flex-Start A3 nodes have a 7-day lifetime. This runbook documents the **single-replica reschedule** continuity path for node rotation — the model cache PVC (ReadWriteOnce) reattaches in the same zone with a brief serving gap during replacement node provisioning.

> **User-facing version:** the same 7-day expiry / node-rotation topic is covered for operators in [docs/guides/02e-verify-teardown.md](../../docs/guides/02e-verify-teardown.md) (Step 10).

> ### Updated 2026-08-05 — current target
>
> | | |
> |---|---|
> | Cluster | `hypercomputer-a3-tcpxo` |
> | Zone | `asia-southeast1-c` |
> | Pool | `a3-mega-tcpxo-flex-pool` (3 × `a3-megagpu-8g`) |
> | Accelerator label | `nvidia-h100-mega-80gb` |
>
> ```bash
> gcloud container clusters get-credentials hypercomputer-a3-tcpxo \
>   --location asia-southeast1-c --project hdlab-elideng
> ```
>
> **Three things differ from the single-node DWS assumption below.**
>
> **1. Re-grabbing is automated.** The [capacity watchdog](../../docs/guides/01-architecture.md#6b-capacity-watchdog)
> (Cloud Run Job `gpu-flex-watchdog`, every 15 min) recreates the pool when it hits **zero**
> nodes, which is what clears autoscaler scale-up backoff. Step 1's manual pre-provisioning is
> usually unnecessary — but check the watchdog actually ran:
> ```bash
> gcloud run jobs executions list --job=gpu-flex-watchdog --region=us-central1 --limit=3
> ```
>
> **2. ⚠️ Re-grant SSH after every rotation.** Node SSH access is *instance-level* IAM, so a
> replacement node comes up with an **empty** policy and the team silently loses access with no
> error until someone tries to connect:
> ```bash
> deploy/ops/grant-node-ssh.sh
> ```
>
> **3. ⚠️ Verify the fabric came back, not just the node.** A pool recreated **without** the 8
> `--additional-node-network` flags looks completely healthy — pods schedule, nothing errors —
> while NCCL has silently fallen back to a single gVNIC at roughly **1/13th** the bandwidth.
> The watchdog stores those flags per-pool, but verify after any manual recreate:
> ```bash
> kubectl get network.networking.gke.io          # expect gpu-net-0 .. gpu-net-7
> kubectl get ds -n kube-system | grep -E 'tcpxo|device-injector'
> # and from inside a GPU pod:
> ls /sys/class/net          # expect eth0..eth8 (9 NICs)
> ls /dev/aperture_devices   # expect 8 entries
> ```
>
> **4. Holders must be re-armed too.** All 24 GPUs are normally held. After rotation confirm
> `gpu-holder-tcpxo` / `gpu-holder-tcpxo-partial` are Running (see `rearm-holder.sh`) — an
> unheld Flex node can be reclaimed within minutes.

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
