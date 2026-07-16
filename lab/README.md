# SP-0 Foundation / IaC — Lab Environment

This directory contains the Infrastructure-as-Code (IaC) foundation for the AI Hypercomputer Reproduction Lab. It provisions a **regional GKE cluster** with a **matrix of GPU node pools** (L4, A100, H100, H200, B200), each DWS flex-start autoscaled 0→N and defaulting to zero, with correct networking (gVNIC, GPUDirect-TCPX/TCPXO, RDMA/CX-7) baked in per GPU type.

**Key features:**
- **Kueue** with per-team namespaces/quotas and DWS ProvisioningRequest admission
- **Fabric-ready nodes:** GPUDirect/RDMA installers and DCGM installed at deploy time
- **Cost guardrails:** pools default to zero; one-command teardown; zero-GPU cost assertion
- **Smoke gate:** per-GPU-type sanity tests (DCGM + NCCL all-reduce) to validate foundation health

**Status:** Validated offline (Terraform static checks, blueprint lint, manifest validation). Full end-to-end verification requires deployment to a live project with GPU quota and DWS capacity.

---

## Prerequisites

### 1. Project and billing

- A GCP project with billing enabled
- Appropriate IAM permissions (project editor or equivalent) to create clusters, VPCs, and node pools

### 2. API enablement

The `make bootstrap` step enables required APIs, but you can enable them manually if needed:
```bash
gcloud services enable compute.googleapis.com container.googleapis.com \
  networkmanagement.googleapis.com iam.googleapis.com --project <project-id>
```

### 3. Quota increases

**Critical:** Default quotas are insufficient for this lab. Request increases for:

| Quota | Default | Required | Reason |
|-------|---------|----------|--------|
| **VPCs (Networks)** | 5 | **≥10** | System VPC + 8 GPU VPCs (TCPXO) + RDMA VPC |
| **Per-GPU quotas** | varies | ≥8 per enabled pool | Each node has 8 GPUs; request per region |

**GPU quota metrics per pool:**
- L4: `NVIDIA_L4_GPUS`
- A100: `NVIDIA_A100_80GB_GPUS`
- H100 High: `NVIDIA_H100_GPUS`
- H100 Mega: `NVIDIA_H100_MEGA_GPUS`
- H200 Ultra: `NVIDIA_H200_GPUS`
- B200: `NVIDIA_B200_GPUS`

Check current quotas:
```bash
gcloud compute regions describe <region> --project <project-id> --format="table(quotas.metric,quotas.limit)"
```

**Note:** H200 and B200 are available in limited regions and may require private preview access. Contact Google Cloud support for availability.

### 4. VPC quota for advanced interconnects

**TCPXO (h100-mega) and RDMA (h200-ultra, b200) require ≥10 VPCs per project.** The preflight script (`make bootstrap`) checks this and fails if insufficient. Request a VPC quota increase to 15-20 networks to accommodate system + GPU + RDMA VPCs.

### 5. Cluster Toolkit (`gcluster`) installation

Install Cluster Toolkit to render blueprints:
```bash
# Install gcluster (version 2.x+)
# See https://cloud.google.com/cluster-toolkit/docs/deploy/cluster-toolkit
curl -O https://github.com/GoogleCloudPlatform/cluster-toolkit/releases/download/v2.x/gcluster
chmod +x gcluster
sudo mv gcluster /usr/local/bin/
gcluster --version
```

**Important:** Cluster Toolkit module sources and field names vary by version. The blueprints in this repo reference module paths (e.g., `modules/network/multivpc`, `modules/network/rdma-vpc`) that must be verified against the installed `gcluster` version. Check the Cluster Toolkit repository for exact module names if you encounter blueprint rendering errors.

### 6. GCS state bucket

Terraform state is stored in a GCS bucket (versioned, regional). The `make bootstrap` step creates the bucket (`gs://<project-id>-lab-tfstate`) automatically, but you can create it manually if needed:
```bash
gsutil mb -p <project-id> -l <region> gs://<project-id>-lab-tfstate
gsutil versioning set on gs://<project-id>-lab-tfstate
```

---

## Quick Start

### 1. Bootstrap (one-time setup)

```bash
cd lab
make bootstrap PROJECT=<project-id> REGION=<region>
```

This step:
- Enables required APIs
- Creates the GCS state bucket for Terraform
- Runs preflight checks (VPC quota, per-GPU quota for enabled pools)

### 2. Configure region and pools

Edit `env/<region>.tfvars` (create if needed, using `env/us-central1.tfvars` as a template):

```hcl
project_id    = "<your-project-id>"
region        = "us-central1"
enabled_pools = ["l4", "a100", "h100-high"]  # Enable only pools with quota/capacity
teams         = ["team-a"]
max_run_duration_seconds = 86400  # 24h default; adjust as needed
```

**Pool availability by region:**
- L4, A100: widely available
- H100 (high/mega): select regions (us-central1, europe-west4, asia-southeast1, etc.)
- H200, B200: limited regions, private preview

**Note:** `max_run_duration_seconds` is a cost guardrail (default 24h). Jobs exceeding this duration are terminated. Adjust based on expected workload runtime.

### 3. Deploy the cluster

```bash
make up REGION=<region> ENABLED_POOLS=l4,a100,h100-high
```

This step:
- Composes the full blueprint from `blueprints/base-cluster.yaml` + enabled pool blueprints
- Renders the blueprint to Terraform via `gcluster create`
- Runs `terraform init` (GCS backend) → `terraform validate` → `terraform plan` → `terraform apply`
- Deploys:
  - System VPC + GPU VPCs (TCPX/TCPXO) + RDMA VPC (conditional on pool types)
  - GKE cluster (regional, REGULAR release channel)
  - System node pool (2-3 nodes, e2-standard-8, always-on)
  - GPU node pools (DWS flex-start, autoscale 0→N, initial 0)
  - Kueue (ResourceFlavors, ClusterQueues, LocalQueues, ProvisioningConfig)
  - DCGM exporter (for observability)

**Deploy time:** 10-15 minutes for cluster creation; add 5-10 minutes for Kueue/DCGM manifests.

**Post-deploy:** Set kubectl context:
```bash
gcloud container clusters get-credentials hpc-lab --region <region> --project <project-id>
kubectl get nodes
```

### 4. Run smoke tests

Validate the foundation with a per-GPU-type smoke test:

```bash
make smoke GPU=h100-high
```

This step:
- Submits a Kueue-managed job to the `team-a` namespace
- Kueue creates a DWS ProvisioningRequest to provision 1 node
- Once provisioned, the job runs:
  - `dcgmi diag -r 1` (GPU health check)
  - `all_reduce_perf -b 512M -e 2G -g 8` (8-GPU NCCL all-reduce)
- Waits up to 45 minutes for completion (DWS provisioning + job runtime)
- Displays the last 30 lines of logs (including NCCL bandwidth results)
- Cleans up the job (node is released via Kueue)

**Repeat for each enabled GPU type:**
```bash
make smoke GPU=l4
make smoke GPU=a100
make smoke GPU=h100-mega
```

**Note:** Smoke tests incur GPU cost (1 node × job duration, typically 5-10 minutes after provisioning). DWS provisioning may take 5-15 minutes depending on regional capacity.

### 5. Teardown

**Scale to zero (keep cluster):**
```bash
make down
make verify-cost
```
This deletes all ProvisioningRequests and scales GPU pools to 0 nodes. System pool remains running (minimal cost). Run `make verify-cost` to assert zero GPU nodes and zero PRs (cost invariant).

**Full destroy (delete cluster):**
```bash
make down DESTROY=1
```
This runs `terraform destroy`, deleting the cluster, VPCs, and all resources. State is preserved in GCS.

---

## Architecture Overview

### VPC and networking design

| Network | Purpose | Used by |
|---------|---------|---------|
| **system-net** | GKE control plane, system pool, L4/A100 pools, k8s/DWS traffic | All nodes |
| **gpu-net-{0..7}** (8 VPCs) | GPUDirect-TCPX/TCPXO fabric for A3 High/Mega | h100-high (first 4), h100-mega (all 8) |
| **rdma-net** | GPUDirect-RDMA (CX-7) fabric for A3 Ultra/A4 | h200-ultra, b200 |

**Design notes:**
- **System VPC** carries cluster control plane, CPU pools, and single-NIC GPU pools (L4/A100 use gVNIC on system network).
- **GPU VPCs** (TCPX/TCPXO) are created only when `h100-high` or `h100-mega` are enabled. Each VPC is attached as an additional node network. A3 High uses the first 4 GPU NICs; A3 Mega uses all 8.
- **RDMA VPC** uses the `mrdma` network profile and is created only when `h200-ultra` or `b200` are enabled. GPUDirect-RDMA binaries are installed via the Cluster Toolkit RDMA installer daemonset.
- **Firewall rules** allow intra-cluster traffic and GPU fabric traffic, scoped by network tags. No public GPU node exposure.

### GPU pool topology

| Pool | Machine type | GPU | Interconnect | NICs | Autoscale |
|------|-------------|-----|--------------|------|-----------|
| `l4` | g2-standard-96 | 8× L4 | none | 1 gVNIC (system) | 0→2 |
| `a100` | a2-ultragpu-8g | 8× A100 80GB | NVSwitch | 1 gVNIC (system) | 0→2 |
| `h100-high` | a3-highgpu-8g | 8× H100 | NVSwitch + TCPX | 1 system + 4 GPU NICs | 0→2 |
| `h100-mega` | a3-megagpu-8g | 8× H100 | NVSwitch + TCPXO | 1 system + 8 GPU NICs | 0→2 |
| `h200-ultra` | a3-ultragpu-8g | 8× H200 | NVSwitch + RDMA (CX-7) | 1 system + RDMA NICs | 0→2 |
| `b200` | a4-highgpu-8g | 8× B200 | NVSwitch + RDMA (CX-7) | 1 system + RDMA NICs | 0→2 |

**Labels applied to all GPU pools:**
- `lab.gpu/type`: pool id (e.g., `l4`, `h100-mega`)
- `lab.gpu/interconnect`: interconnect type (e.g., `tcpxo`, `rdma`)

These labels are used by Kueue ResourceFlavors for workload routing.

### Kueue topology

- **ResourceFlavors:** one per GPU type (l4, a100, h100-high, h100-mega, h200-ultra, b200), keyed to `lab.gpu/type` node labels
- **ClusterQueues:** one per GPU type (cq-l4, cq-a100, etc.), all in a shared cohort for borrowing
- **LocalQueues:** per team namespace (e.g., lq-l4 in team-a), with per-team quotas
- **ProvisioningConfig:** DWS admission check enabled for all ClusterQueues; Kueue auto-creates/deletes ProvisioningRequests

**Cost model:**
- GPU pools autoscale 0→N, default **0** (no idle nodes)
- Jobs trigger DWS provisioning via Kueue LocalQueue admission
- `maxRunDuration` (default 24h, configurable in `env/<region>.tfvars`) terminates long-running jobs (cost guardrail)
- Job TTL (300s) cleans up completed/failed jobs
- Invariant: **no GPU node runs without an admitted Kueue workload**

---

## Makefile Targets

| Target | Description |
|--------|-------------|
| `bootstrap` | One-time setup: enable APIs, create state bucket, run preflight checks |
| `compose` | Assemble the full blueprint from base + enabled pool blueprints |
| `render` | Run `gcluster create` to render Terraform from the composed blueprint |
| `plan` | Run `terraform init` + `validate` + `plan` (does not apply) |
| `up` | Full deploy: render → plan → apply (idempotent) |
| `down` | Teardown: scale GPU pools to 0 (default) or full destroy (with `DESTROY=1`) |
| `verify-cost` | Assert zero GPU nodes and zero ProvisioningRequests (cost invariant) |
| `smoke` | Run per-GPU-type smoke test (DCGM + NCCL all-reduce); requires `GPU=<type>` |

**Examples:**
```bash
make bootstrap PROJECT=my-project REGION=us-central1
make up REGION=us-central1 ENABLED_POOLS=l4,a100,h100-high
make smoke GPU=h100-high
make down
make verify-cost
make down DESTROY=1
```

---

## Day-2 Operations

### Add a new region

1. Create a new tfvars file: `env/<new-region>.tfvars`
2. Set `region`, `project_id`, `enabled_pools` (only enable pools with quota/capacity in that region)
3. Run `make up REGION=<new-region>`

Each region is a separate Terraform deployment (separate state prefix in GCS).

### Add or remove GPU pools

1. Edit `env/<region>.tfvars` to change `enabled_pools`
2. Re-run `make up REGION=<region>`
3. Terraform will add/remove node pools and VPCs as needed (idempotent)

**Note:** Removing a pool does NOT delete provisioned nodes; scale the pool to 0 first via `make down` or `gcloud container clusters resize`.

### Upgrade Cluster Toolkit

1. Upgrade `gcluster` binary to the new version
2. Verify module sources in `blueprints/base-cluster.yaml` and `blueprints/pools/*.yaml` match the new version's module paths (e.g., `modules/network/multivpc` vs. `modules/network/multi-vpc`)
3. Re-render: `make render`
4. Review the Terraform plan for unexpected changes: `make plan`
5. Apply if safe: `make up`

---

## Troubleshooting

### Capacity waits (DWS provisioning)

**Symptom:** `make smoke` hangs; ProvisioningRequest stuck in `Pending` or `Provisioning` state for >15 minutes.

**Cause:** DWS cannot find capacity for the requested GPU type in the region. This is a regional supply issue, not a configuration error.

**Workaround:**
- Try a different region with better GPU availability
- Reduce the number of nodes requested (smoke test only needs 1 node)
- Wait for capacity to become available (may take hours or days)

**Related:** `../bugfixes/0001-dws-zone-requests-not-zone-pinned.md` (when that file is created) documents zone-pinning issues with manual DWS requests. Kueue's DWS admission check does not pin zones, allowing DWS to search across all zones in the region.

### Node reclaim race (historical issue, now fixed)

**Symptom (historical):** DWS provisions a node, but it is reclaimed after 10 minutes even though the workload is still running.

**Cause:** Manual DWS ProvisioningRequests not tied to pod lifecycle; consumer-holder pattern requires zone-pinned holders to prevent reclaim.

**Fix:** Kueue's DWS admission check (configured in `manifests/kueue/provisioning-config.yaml`) owns the ProvisioningRequest lifecycle. Kueue holds the request for the workload's lifetime, preventing premature reclaim. **This issue does not affect the SP-0 foundation.**

**Related:** `../bugfixes/0002-dws-a3-node-reclaimed-after-10min.md` (when that file is created) documents the manual holder pattern and why Kueue supersedes it.

### Blueprint rendering fails

**Symptom:** `make render` fails with "module not found" or "field X not recognized".

**Cause:** Cluster Toolkit module sources or field names changed in a new `gcluster` version.

**Fix:**
1. Check the installed `gcluster` version: `gcluster --version`
2. Consult the Cluster Toolkit repository for exact module names and field schemas for your version
3. Update `blueprints/base-cluster.yaml` and `blueprints/pools/*.yaml` with the correct module sources and settings
4. Re-run `make render`

**Notes in code:** The blueprint files contain comments flagging version-specific fields (e.g., `multivpc` vs. `multi-vpc`, `rdma-vpc` settings).

### Terraform apply fails (state lock or drift)

**Symptom:** `terraform apply` fails with "state lock" error or "resource already exists" error.

**Cause:** Multiple concurrent applies, or resources were created outside Terraform (manual changes).

**Fix (state lock):**
```bash
# Break the lock (only if you're sure no other apply is running)
cd build/hpc-lab/primary
terraform force-unlock <lock-id>
```

**Fix (drift):**
```bash
# Import the drifted resource into state
terraform import <resource-type>.<resource-name> <resource-id>
# Or delete the resource and let Terraform recreate it
gcloud <command> delete <resource-id>
```

### Smoke test times out

**Symptom:** `make smoke GPU=<type>` times out after 45 minutes.

**Cause:** DWS provisioning took too long, or the NCCL test failed to complete.

**Debug:**
```bash
# Check ProvisioningRequest status
kubectl get provisioningrequests -A
kubectl describe provisioningrequest <pr-name> -n team-a

# Check job status
kubectl get jobs -n team-a
kubectl describe job nccl-smoke-<gpu-type> -n team-a

# Check pod logs
kubectl logs job/nccl-smoke-<gpu-type> -n team-a
```

**Common causes:**
- DWS capacity exhausted (wait or try another region)
- DCGM diagnostic failed (GPU unhealthy; check node logs)
- NCCL test failed (fabric issue; check GPUDirect/RDMA installer pods)

---

## Deploy-time Caveats

These are known limitations documented in the code and spec:

1. **Cluster Toolkit module sources** (e.g., `multivpc`, `rdma-vpc`) vary by `gcluster` version. Verify against the installed version before deploying.
2. **Billing account variable** for the cost module: currently not parameterized in tfvars. If the cost module requires a billing account ID, add it to `env/<region>.tfvars` and pass via `-var billing_account=<id>`.
3. **kubectl context for module local-execs:** The Kueue and DCGM Terraform modules use `local-exec` provisioners to `kubectl apply` manifests. Ensure your kubectl context is set to the target cluster before running `make up` (Terraform will fail if kubectl is not configured).
4. **H200/B200 private preview:** H200 and B200 GPUs may require allowlist access. Contact Google Cloud support if you encounter "quota exceeded" errors despite having quota approved.

---

## User Acceptance Verification

After deploying, run the full acceptance checklist in `VERIFY-SP0.md` to confirm the foundation is working correctly. The checklist covers:

1. Cluster is up and system pool is healthy
2. GPU pools exist and are zero-scaled
3. Kueue is healthy (flavors, queues, provisioning config)
4. DWS provisioning works via Kueue (no reclaim race)
5. Node comes up fabric-ready (DCGM + GPUDirect/RDMA)
6. NCCL sanity check passes (8-GPU all-reduce)
7. Observability hooks present (DCGM metrics scraped)
8. Teardown + cost invariant validated (zero GPU nodes/PRs)

**SP-0 is not complete until all applicable steps in `VERIFY-SP0.md` pass.**

---

## Next Steps

- **SP-1:** Driver/CUDA/NCCL version matrix and image builds
- **SP-2:** NCCL tuning and `nccl-tests` benchmarking (multi-node, fabric validation)
- **SP-3:** Training workload reproductions
- **SP-4:** Observability dashboards (build on DCGM metrics)
- **SP-5:** Fault injection and resilience testing
- **SP-6:** Reproduction control plane

---

## Related Documentation

- **SP-0 Design Spec:** `../docs/superpowers/specs/2026-07-16-sp0-foundation-iac-design.md`
- **Program Overview:** `../docs/superpowers/specs/2026-07-16-hypercomputer-lab-program-overview.md`
- **Bugfixes:** `../bugfixes/` (to be populated with DWS capacity and reclaim issue documentation)
- **User Acceptance Checklist:** `VERIFY-SP0.md` (this directory)

---

## License

This repository is licensed under [LICENSE]. For questions or contributions, contact the AI Hypercomputer Reproduction Lab team.
