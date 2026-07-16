# SP-0 User Acceptance Verification Checklist

This checklist corresponds to the 8 acceptance steps from the SP-0 spec (section 8). Run these commands after `make up` to verify that the foundation is working correctly. SP-0 is not considered complete until every applicable step passes.

**Note:** Steps 4-6 require a live cluster with DWS capacity for the tested GPU type. This checklist is validated offline in the SP-0 implementation phase; full end-to-end verification requires deploying to a project with appropriate quota and capacity.

## Prerequisites

- Cluster deployed via `make up REGION=<region>`
- kubectl context set to the deployed cluster: `gcloud container clusters get-credentials hpc-lab --region <region>`
- At least one GPU pool enabled in `env/<region>.tfvars` for steps 4-6

---

## 1. Cluster is up

**Command:**
```bash
gcloud container clusters describe hpc-lab --region <region> --format="value(status)"
kubectl get nodes -l cloud.google.com/gke-nodepool=system
```

**Pass criterion:** Cluster status is `RUNNING`; system pool nodes are `Ready`.

---

## 2. GPU pools exist and are zero-scaled

**Command:**
```bash
gcloud container node-pools list --cluster=hpc-lab --region=<region> --format="table(name,config.machineType,autoscaling.minNodeCount,autoscaling.maxNodeCount,status)"
gcloud container node-pools describe <pool-name> --cluster=hpc-lab --region=<region> --format="value(initialNodeCount)"
```

**Pass criterion:** Every enabled GPU pool (l4, a100, h100-high, h100-mega, h200-ultra, b200) is listed with `initialNodeCount=0` and `status=RUNNING`. No idle GPU nodes at rest (zero cost baseline).

---

## 3. Kueue is healthy

**Command:**
```bash
kubectl get clusterqueues,localqueues,resourceflavors -A
kubectl get pods -n kueue-system
```

**Pass criterion:**
- ResourceFlavors exist for each enabled GPU type (l4, a100, h100-high, h100-mega, h200-ultra, b200)
- ClusterQueues exist for each enabled GPU type (cq-l4, cq-a100, cq-h100-high, etc.)
- LocalQueues exist in team namespaces (e.g., lq-l4 in team-a)
- Kueue pods in `kueue-system` namespace are `Running` and `Ready`

---

## 4. DWS provisioning works via Kueue

**Command (using h100-high as example):**
```bash
make smoke GPU=h100-high
# Alternatively, submit manually:
kubectl apply -f manifests/smoke/nccl-smoke.yaml -n team-a
kubectl get provisioningrequests -A -w
```

**Pass criterion:**
- Kueue automatically creates a ProvisioningRequest for the queued job
- ProvisioningRequest transitions to `Provisioned=True` (may take 5-15 minutes depending on capacity)
- Pod schedules to the provisioned node
- Node is NOT reclaimed while the job is running (this validates the fix for bugfix 0002 — Kueue's admission check holds the node for the workload lifecycle)

---

## 5. Node comes up fabric-ready

**Command (on a provisioned node):**
```bash
# Identify the node
kubectl get nodes -l lab.gpu/type=<gpu-type>

# Verify DCGM diagnostic passes
kubectl exec -it <job-pod> -n team-a -- dcgmi diag -r 1

# Verify GPUDirect/RDMA installers are running (for fabric-enabled types)
# For h100-high/h100-mega (TCPX/TCPXO):
kubectl get pods -A -l app=gpudirect-tcpx
# For h200-ultra/b200 (RDMA):
kubectl get pods -A -l app=nvidia-driver-installer

# Verify expected NICs are present
# h100-high: 4 GPU NICs + 1 system NIC
# h100-mega: 8 GPU NICs + 1 system NIC
# h200-ultra/b200: RDMA NICs with mrdma profile
kubectl exec -it <job-pod> -n team-a -- ip link show
```

**Pass criterion:**
- `dcgmi diag -r 1` exits with code 0 (all GPUs healthy)
- Installer daemonset pods are `Running` on the node (for fabric-enabled types)
- Expected NICs are present and configured correctly for the GPU type

---

## 6. NCCL sanity check

**Command:**
```bash
make smoke GPU=<type>
kubectl logs job/nccl-smoke-<type> -n team-a | tail -30
```

**Pass criterion:**
- The smoke job runs an 8-GPU NCCL all-reduce (`all_reduce_perf -b 512M -e 2G -g 8`)
- Job completes successfully (status: `Completed`)
- Logs show non-zero bus bandwidth (busbw) values, indicating successful NCCL communication
- For fabric-enabled types (h100-high, h100-mega, h200-ultra, b200), the smoke test validates GPUDirect/RDMA is functional

**Note:** A full 2-node all-reduce test requires provisioning 2 nodes. The single-node 8-GPU test provides basic NCCL sanity; multi-node fabric validation is part of SP-2 (benchmarking).

---

## 7. Observability hooks present

**Command:**
```bash
kubectl get pods -n dcgm-system
kubectl get servicemonitor -A
# Verify DCGM metrics endpoint (requires port-forward or in-cluster query)
kubectl port-forward -n dcgm-system svc/dcgm-exporter 9400:9400 &
curl -s http://localhost:9400/metrics | grep DCGM_FI_DEV_GPU_UTIL
```

**Pass criterion:**
- DCGM exporter pods are `Running` in `dcgm-system` namespace
- DCGM metrics endpoint is reachable and returning GPU metrics
- This confirms SP-4 (dashboards) can build on the observability foundation

---

## 8. Teardown and cost invariant

**Command:**
```bash
# Teardown to zero-scale (keep cluster):
make down
make verify-cost

# Full destroy (delete cluster and all resources):
make down DESTROY=1

# Idempotency check (re-run apply is a no-op):
make plan
```

**Pass criterion:**
- After `make down`: zero GPU nodes remain (`gcloud compute instances list --filter=machineType~(a2|a3|a4|g2)` returns empty)
- After `make down`: zero active ProvisioningRequests (`kubectl get provisioningrequests -A` returns empty)
- `make verify-cost` passes (runs `scripts/assert_zero_gpu.py` and confirms zero GPU spend)
- After `make down DESTROY=1`: cluster is deleted; Terraform state shows no resources
- Re-running `make plan` after `make up` shows no drift (idempotency — validates no unmanaged config changes)

---

## Per-GPU-type coverage

Only the pools enabled in `env/<region>.tfvars` need to pass steps 4-6. For example:
- If `enabled_pools = ["l4", "a100"]`, only test `make smoke GPU=l4` and `make smoke GPU=a100`
- H200/B200 pools require regional availability and quota; skip if not enabled

---

## Known issues and workarounds

- **Capacity waits (step 4):** DWS ProvisioningRequests may wait indefinitely if the region lacks capacity for the requested GPU type. This is documented in `../bugfixes/0001-dws-zone-requests-not-zone-pinned.md`.
- **Reclaim race (step 4):** Kueue's DWS admission check (configured in `manifests/kueue/provisioning-config.yaml`) prevents premature node reclaim by holding the ProvisioningRequest for the workload's lifecycle. This supersedes the manual zone-pinned holder pattern documented in `../bugfixes/0002-dws-a3-node-reclaimed-after-10min.md`.

---

## Summary

When all applicable steps pass:
- The foundation is healthy and reproducible
- GPU pools are correctly provisioned via Kueue + DWS
- Nodes come up fabric-ready with GPUDirect/RDMA working
- Observability is in place for SP-4
- Teardown returns to zero GPU cost (cost invariant validated)

**Next steps:** Proceed to SP-1 (driver/CUDA/NCCL image matrix) and SP-2 (NCCL benchmarking).
