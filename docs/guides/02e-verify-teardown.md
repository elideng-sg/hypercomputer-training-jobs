# Deployment Part 5 — Verify, Rotate & Teardown

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → **5. Verify & Teardown**

---

**Part 5 of the deployment series** — the final part. Assumes you have completed [Parts 1–4](02a-cluster-setup.md).

This part verifies the whole stack end-to-end, then covers day-2 operations: tearing down workloads while keeping the node held, the 7-day Flex-Start expiry and node rotation, durable (>7-day) capacity, and full cleanup. It ends with troubleshooting, a command quick-reference, and cost estimates.

---

## Step 8: Verify the whole stack

### 8.1 Node and GPU check

```bash
kubectl get nodes -L cloud.google.com/gke-accelerator
```

**Expected output:**
```
NAME                                                  STATUS   ROLES    AGE   ACCELERATOR
gke-hypercomputer-a3-default-pool-xxxx-yyyy           Ready    <none>   2h    <none>
gke-hypercomputer-a3-a3-h100-dws-pool-16664d9c-hhp6   Ready    <none>   1h    nvidia-h100-80gb
```

### 8.2 Check GPU allocation

```bash
kubectl describe node <gpu-node-name> | grep -A10 "Allocated resources"
```

**Expected output:**
```
Allocated resources:
  Resource           Requests        Limits
  nvidia.com/gpu     3               3
```

**What this means:** 3 of 8 GPUs are allocated:
- **2 GPUs** — vLLM inference (tensor parallel)
- **1 GPU** — a running GPU notebook (if you launched one)
- **5 GPUs remain** for additional notebooks

### 8.3 Check vLLM health

```bash
kubectl exec -n inference deploy/qwen3-vllm -- nvidia-smi --query-gpu=index,memory.used --format=csv
```

**Expected output:**
```
index, memory.used [MiB]
0, 77583 MiB
1, 77583 MiB
```

**What this means:** GPUs 0 and 1 are loaded with the Qwen3-32B model (~77GB per GPU).

### 8.4 End-to-end test (notebook → vLLM)

1. Open a GPU notebook in JupyterHub
2. Run the OpenAI client cell (from Step 7.6)
3. Verify you get a response from `qwen3-32b`

**Success criteria:** All three layers work:
- GPU node is up and healthy
- vLLM serves inference requests
- Notebooks can call vLLM and access GPUs

---

## Step 9: Teardown and re-arm holder

When you're done with the inference service but want to keep the GPU node for other work (or just to hold capacity during the 7-day window):

### 9.1 Tear down vLLM and re-arm holder

```bash
kubectl -n inference delete deploy qwen3-vllm
kubectl -n default scale deploy/a3-holder-zone-a --replicas=1
```

**What this does:**
- Deletes the vLLM Deployment (pod terminates)
- Scales the holder back to 1 replica — it binds immediately (same node) and keeps the node from being reclaimed
- **House rule:** Never leave the DWS GPU node idle/unheld (cost is not a concern; **node reclaim is**)

**Verify holder is back:**
```bash
kubectl get pods -n default -l app=a3-holder-zone-a -o wide
```

**Expected output:**
```
NAME                                  READY   STATUS    RESTARTS   AGE   NODE
a3-holder-zone-a-xxxxxxxxxx-yyyyy     1/1     Running   0          10s   gke-hypercomputer-a3-a3-h100-dws-pool-16664d9c-hhp6
```

### 9.2 Full teardown (scale to zero, release capacity)

To **give up the GPU node** and return to 0 nodes (stop paying for H100 time):

```bash
# Delete all workloads on the GPU node
kubectl -n inference delete deploy qwen3-vllm
kubectl -n default delete deploy a3-holder-zone-a

# Delete the ProvisioningRequest (releases the node)
kubectl delete provisioningrequest a3-h100-req-zone-a -n default
```

**What this happens:**
- Deleting the ProvisioningRequest tells GKE to release the capacity
- The node will be terminated within ~5 minutes
- The node pool remains at 0 nodes (no cost until you re-provision)

**Verify node is gone:**
```bash
kubectl get nodes -L cloud.google.com/gke-accelerator
```

**Expected output:**
```
NAME                                        STATUS   ROLES    AGE   ACCELERATOR
gke-hypercomputer-a3-default-pool-xxxx-yyyy Ready    <none>   3h    <none>
```

**To re-provision later:** Re-apply the DWS request and holder (Step 4).

---

## Step 10: Node rotation and the 7-day expiry

### 10.1 The 7-day hard cap

DWS Flex-Start nodes have a **hard 7-day `maxRunDurationSeconds`**. No holder or trick extends it past 7 days. When the window expires (~2026-07-23 for the example node provisioned on 2026-07-16), GKE **deletes the node** and the ProvisioningRequest returns to `PROVISIONED=False`.

**What happens to your workload:**
- The inference Deployment's pod is **evicted**
- The pod becomes `Pending` again (no node to schedule on)
- **If the PVC is ReadWriteOnce (single-node)**, it stays in the same zone and will **reattach** if you provision a replacement node in that zone
- **Serving gap:** Minutes to hours (depending on DWS capacity availability)

**This is continuity, not zero-downtime.**

### 10.2 Continuity plan: single-replica reschedule

**Days 5-6 before expiry:**
1. Submit a **new DWS request** for a replacement node in the **same zone** (to minimize reattach latency)
2. Deploy a holder for the new request (don't touch the old one yet)
3. Wait for the replacement node to provision

**On day 7 (or when the old node expires):**
1. The old node is terminated automatically (or cordon/drain it manually)
2. The vLLM pod becomes `Pending`
3. Once the new node is `Ready`, the pod **reschedules** automatically and the PVC **reattaches**
4. Service resumes after model reload (~2-5 minutes)

**Why the PVC reattaches:** `ReadWriteOnce` Persistent Disks can only attach to one node at a time, but **can reattach to a different node in the same zone**. GKE handles this automatically. The cached model weights persist across the rotation.

**Related:** See `deploy/ops/node-rotation-runbook.md` in the repo for the full procedure, including the PDB caveat (a `minAvailable: 1` PDB on a single-replica Deployment **blocks voluntary drain** until you scale up or delete the PDB).

### 10.3 Zero-downtime overlap (future enhancement)

True **zero-downtime rotation** (2 replicas across two nodes during overlap) requires:
- **ReadWriteMany** model cache (e.g., Filestore NFS) so the PVC can attach to both nodes
- **Pod anti-affinity** to force replicas onto separate nodes
- **2 DWS requests** active at once (2× H100 cost during overlap window)

**This is not configured in the current deployment.** The documented path is single-replica reschedule with a brief gap.

### 10.4 Durable capacity (>7 days)

For capacity that must live **longer than 7 days** (e.g., a 6-month lab):

1. **Create a Compute Engine reservation** (contact your GCP account team):
   ```bash
   gcloud compute reservations create a3-h100-res \
     --project=$PROJECT_ID \
     --zone=us-central1-a \
     --require-specific-reservation \
     --vm-count=3 \
     --machine-type=a3-highgpu-8g \
     --accelerator=type=nvidia-h100-80gb,count=8
   ```

2. **Create a standard (non-DWS) node pool** that consumes the reservation:
   ```bash
   gcloud container node-pools create a3-h100-reserved-pool \
     --cluster hypercomputer-a3-cluster \
     --region us-central1 \
     --machine-type a3-highgpu-8g \
     --num-nodes 3 \
     --min-nodes 3 \
     --max-nodes 3 \
     --node-locations us-central1-a \
     --reservation-affinity specific \
     --reservation a3-h100-res \
     --accelerator type=nvidia-h100-80gb,count=8,gpu-driver-version=latest \
     --node-labels cloud.google.com/gke-accelerator=nvidia-h100-80gb \
     --node-taints nvidia.com/gpu=present:NoSchedule
   ```

**Key differences from DWS:**
- **No run-duration cap** — nodes stay up indefinitely (or until you delete the pool/reservation)
- **Guaranteed capacity** — no wait for provisioning (already reserved)
- **Higher cost** — on-demand pricing, bills 24/7 whether used or not
- **No holder needed** — `min-nodes=3` keeps the pool up always

**See `lab/RESERVATIONS.md`** for the full guide, including why you should book all 3 nodes in **one zone with COMPACT placement** (for multi-node fabric work).

**Migration:** You **cannot** convert Flex-Start → reserved. Stand up the reserved pool separately and move workloads/checkpoints **before** the 7-day expiry, then release the Flex-Start request.

---

## Step 11: Clean up everything

To **delete the entire stack** and stop all charges:

```bash
# Delete workloads
kubectl delete namespace inference
kubectl delete namespace jupyter
kubectl delete provisioningrequest a3-h100-req-zone-a -n default
kubectl delete deploy a3-holder-zone-a -n default

# Delete the GPU node pool
gcloud container node-pools delete a3-h100-dws-pool \
  --cluster hypercomputer-a3-cluster \
  --region us-central1 \
  --project $PROJECT_ID \
  --quiet

# Delete the cluster
gcloud container clusters delete hypercomputer-a3-cluster \
  --region us-central1 \
  --project $PROJECT_ID \
  --quiet
```

**Time:** ~10 minutes.

**What this does:**
- Deletes all Kubernetes resources (namespaces, pods, services, PVCs)
- Deletes the GPU node pool (releases any provisioned nodes)
- Deletes the entire GKE cluster (control plane + default node pool)
- **Persistent Disks** (PVCs) are deleted when their namespace is deleted (default `reclaimPolicy: Delete`)

**Verify cleanup:**
```bash
gcloud container clusters list --project $PROJECT_ID
```

**Expected output:**
```
Listed 0 items.
```

---

## Appendix A: Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| ProvisioningRequest stuck `ACCEPTED=True, PROVISIONED=False` for >1 hour | No H100 capacity in region | Wait, or try a different region (see Step 1) |
| GPU node provisions then vanishes ~10-15 min later | Holder missing consume annotations | Apply corrected `dws-holder.yaml` (Step 4.2) with both `autoscaling.x-k8s.io/*` annotations |
| vLLM pod crash-loops "Engine core initialization failed" | CUDA version mismatch (image CUDA 12.2+ on driver 12.0) | Use `vllm/vllm-openai:v0.8.4` (Step 6.1) |
| vLLM OOM / IPC errors | `/dev/shm` too small (64MB default) | Add 16Gi `/dev/shm` emptyDir volume (Step 6.1) |
| Notebook stuck `Pending` | GPU node full, or missing tolerations | Check GPU allocation (Step 8.2); verify tolerations in `jupyter-values.yaml` (Step 7.2) |
| Model re-downloads on every restart | PVC not mounted / `HF_HOME` unset | Verify `hf-cache` PVC + `HF_HOME=/hf-cache` in Deployment (Step 6.1) |
| Can't reach vLLM/JupyterHub from outside GCP | Services are internal-only | Expected (private IPs). Access via GCP VM in same VPC, or set up Cloud VPN/IAP |
| DWS request never provisions (>24 hours) | Quota not approved, or no capacity | Check quota (Step 1.1); contact GCP support for capacity/quota status |

**Bug reports:** See `bugfixes/` in the repo for documented issues and fixes:
- `bugfixes/0001-dws-zone-requests-not-zone-pinned.md` — why zone selectors matter
- `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md` — the consumer holder pattern

---

## Appendix B: Command quick reference

```bash
# --- Project setup ---
export PROJECT_ID=hdlab-elideng
gcloud config set project $PROJECT_ID
gcloud services enable compute.googleapis.com container.googleapis.com

# --- Cluster access ---
gcloud container clusters get-credentials hypercomputer-a3-cluster \
  --region us-central1 --project $PROJECT_ID

# --- DWS status ---
kubectl get provisioningrequests -A
kubectl get pods -n default -l app=a3-holder-zone-a -o wide

# --- Inference service ---
kubectl get pods -n inference -o wide
kubectl logs -n inference -l app=qwen3-vllm -f
kubectl get svc -n inference qwen3-vllm  # Get internal LB IP
curl http://10.128.0.43:8000/v1/models

# --- JupyterHub ---
kubectl get pods -n jupyter
kubectl get svc -n jupyter proxy-public  # Get internal LB IP

# --- GPU check ---
kubectl get nodes -L cloud.google.com/gke-accelerator
kubectl exec -n inference deploy/qwen3-vllm -- nvidia-smi

# --- Handoff holder -> vLLM ---
kubectl -n default scale deploy/a3-holder-zone-a --replicas=0

# --- Teardown + re-arm holder ---
kubectl -n inference delete deploy qwen3-vllm
kubectl -n default scale deploy/a3-holder-zone-a --replicas=1

# --- Full teardown (release node) ---
kubectl delete provisioningrequest a3-h100-req-zone-a -n default
kubectl delete deploy a3-holder-zone-a -n default
```

---

## Appendix C: Cost estimates (rough, as of 2026-07)

| Resource | Cost (approx) | Notes |
|----------|---------------|-------|
| GKE cluster (control plane) | ~$0.10/hr | Regional cluster management fee |
| Default node pool (3× e2-standard-4) | ~$0.40/hr | System nodes (always-on) |
| **A3 H100 node (a3-highgpu-8g)** | **~$30-35/hr** | 8× H100 80GB (on-demand rate, DWS Flex-Start) |
| Persistent Disk (150Gi SSD) | ~$25/month | Model cache |
| Internal LB (per LB) | ~$0.025/hr | 2 LBs (vLLM + JupyterHub) |

**Key takeaway:** The GPU node is **>95% of the cost**. The 7-day cap (168 hours) = **~$5,000-6,000** for one uninterrupted week. Scale to zero when not in use.

---

← Previous: **[Part 4 — Deploy JupyterHub](02d-deploy-jupyter.md)**

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → **5. Verify & Teardown**

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
