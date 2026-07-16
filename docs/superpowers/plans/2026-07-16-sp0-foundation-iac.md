# SP-0 Foundation / IaC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a deterministic, version-controlled GKE foundation with a DWS-provisioned, zero-scaled GPU node-pool matrix (L4/A100/H100-high/H100-mega/H200/B200), correct per-type networking, Kueue-managed provisioning, cost guardrails, and a user-run verification gate.

**Architecture:** Google Cluster Toolkit (`gcluster`) blueprints generate Terraform for the cluster, GPU pools, and GPUDirect/RDMA networking; our own Terraform + Helm/kustomize modules add Kueue, DCGM, cost controls. One region-parameterized deployment, deployed per region, pools gated by `env/<region>.tfvars`. Remote state in GCS.

**Tech Stack:** Cluster Toolkit (`gcluster`), Terraform (`google`/`google-beta` providers), GKE, Kueue, DCGM, `kubectl`/`kubeconform`, Make, bash, Python (preflight/assertions).

## Global Constraints

- GPU node pools: **DWS flex-start (`--enable-queued-provisioning`), autoscale `0→N`, default size 0.** No idle GPU cost at rest. (verbatim from spec §1, §5)
- **Invariant:** no GPU node runs without an admitted Kueue workload. (spec §5)
- DWS booking is managed by **Kueue's ProvisioningRequest admission check**, NOT a manual holder. (spec §5)
- Pools carry labels `lab.gpu/type` and `lab.gpu/interconnect`; GPU pools carry the DWS/queued taint. (spec §3)
- `maxRunDuration` default **24h**, configurable. (spec §5)
- Per-type networking: gVNIC (L4/A100), GPUDirect-TCPX = system NIC + 4 GPU NICs (`a3-highgpu-8g`), TCPXO = system NIC + 8 GPU NICs (`a3-megagpu-8g`), RDMA/CX-7 `mrdma` (`a3-ultragpu-8g`/`a4-highgpu-8g`). (spec §3, §4)
- GPU/RDMA VPCs created **only** when their pools are enabled for the region. (spec §4)
- Remote Terraform state in GCS; all infra reproducible from `env/<region>.tfvars`. (spec §2)
- No public GPU exposure; firewall scoped by network tags. (spec §4)
- Every phase ships automated (GPU-free) checks **and** a user-run acceptance checklist (`lab/VERIFY-SP0.md`); phase not done until user confirms. (program convention, spec §8)

**Runtime inputs to confirm before Task 2 (open items):** target region + enabled pools; per-GPU quota in the project; team/namespace split for Kueue. Task 1 provides a preflight that surfaces these; defaults: region `us-central1` with pools `l4,a100,h100-high,h100-mega` enabled (H200/B200 disabled until a region with quota is chosen), single team namespace `team-a`.

---

## File Structure

```
lab/
├── Makefile                         # bootstrap / up / down / smoke / verify wrappers
├── README.md                        # howto (Task 9)
├── VERIFY-SP0.md                    # user acceptance checklist (Task 9)
├── env/
│   ├── _example.tfvars              # documented template (Task 1)
│   └── us-central1.tfvars           # first concrete env (Task 2)
├── scripts/
│   ├── bootstrap.sh                 # state bucket + APIs (Task 1)
│   ├── preflight.py                 # quota/availability checks (Task 1)
│   └── assert_zero_gpu.py           # teardown cost-invariant assertion (Task 7)
├── blueprints/
│   ├── base-cluster.yaml            # Cluster Toolkit: net + cluster + system pool (Tasks 2-3)
│   └── pools/                       # per-GPU pool blueprint fragments (Task 4)
├── terraform/
│   ├── backend.tf                   # GCS backend (Task 1)
│   ├── variables.tf                 # enabled_pools, region, teams, ... (Task 2)
│   └── modules/{kueue,dcgm,cost}/   # wrapper modules (Tasks 5-6-7)
├── manifests/
│   ├── dcgm/                        # DCGM exporter + install (Task 5)
│   ├── kueue/                       # flavors, cluster/local queues, PR admission (Task 6)
│   └── smoke/                       # smoke Job template (Task 8)
```

---

### Task 1: `lab/` scaffolding, bootstrap & preflight

**Files:**
- Create: `lab/Makefile`, `lab/env/_example.tfvars`, `lab/scripts/bootstrap.sh`, `lab/scripts/preflight.py`, `lab/terraform/backend.tf`
- Test: `lab/scripts/preflight.py` self-check (dry mode)

**Interfaces:**
- Produces: `make bootstrap` (creates GCS state bucket, enables APIs, runs preflight); `PROJECT`, `REGION`, `ENABLED_POOLS` env/vars consumed by all later tasks; `preflight.py --region <r> --pools <list>` prints quota/availability table and exits non-zero on missing quota.

- [ ] **Step 1: Write the failing check** — add a preflight that must report required quotas.

`lab/scripts/preflight.py`:
```python
#!/usr/bin/env python3
"""Preflight: verify APIs, VPC quota, and per-GPU quota for the enabled pools."""
import argparse, subprocess, sys, json

POOL_GPU = {  # pool -> (accelerator metric, gpus per node)
    "l4": ("NVIDIA_L4_GPUS", 8), "a100": ("NVIDIA_A100_80GB_GPUS", 8),
    "h100-high": ("NVIDIA_H100_GPUS", 8), "h100-mega": ("NVIDIA_H100_MEGA_GPUS", 8),
    "h200-ultra": ("NVIDIA_H200_GPUS", 8), "b200": ("NVIDIA_B200_GPUS", 8),
}
def sh(c): return subprocess.check_output(c, shell=True).decode()
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--region", required=True)
    ap.add_argument("--pools", required=True); ap.add_argument("--project", required=True)
    a = ap.parse_args(); pools = [p for p in a.pools.split(",") if p]
    q = json.loads(sh(f"gcloud compute regions describe {a.region} --project {a.project} --format=json"))
    limits = {x["metric"]: x["limit"] for x in q["quotas"]}
    ok = True
    print(f"{'POOL':12} {'METRIC':24} {'LIMIT':>8} {'NEED':>5}")
    for p in pools:
        metric, need = POOL_GPU[p]; lim = limits.get(metric, 0)
        flag = "" if lim >= need else "  <-- INSUFFICIENT"; ok = ok and lim >= need
        print(f"{p:12} {metric:24} {lim:8.0f} {need:5}{flag}")
    # VPC quota (need ~10 when TCPXO/RDMA enabled)
    print("[i] Ensure Networks-per-project quota >= 10 if h100-mega/h200/b200 enabled.")
    sys.exit(0 if ok else 2)
if __name__ == "__main__": main()
```

- [ ] **Step 2: Run it to verify it fails on missing quota**

Run: `python3 lab/scripts/preflight.py --project <p> --region us-central1 --pools h200-ultra`
Expected: prints table, exits non-zero (H200 quota almost certainly 0).

- [ ] **Step 3: Write `bootstrap.sh` and `Makefile` targets**

`lab/scripts/bootstrap.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
: "${PROJECT:?set PROJECT}"; : "${REGION:?set REGION}"
STATE_BUCKET="gs://${PROJECT}-lab-tfstate"
gcloud services enable compute.googleapis.com container.googleapis.com \
  networkmanagement.googleapis.com iam.googleapis.com --project "$PROJECT"
gsutil ls "$STATE_BUCKET" >/dev/null 2>&1 || gsutil mb -p "$PROJECT" -l "$REGION" "$STATE_BUCKET"
gsutil versioning set on "$STATE_BUCKET"
echo "[+] state bucket ready: $STATE_BUCKET"
```

`lab/Makefile` (initial targets):
```make
PROJECT ?= $(shell gcloud config get-value project 2>/dev/null)
REGION  ?= us-central1
ENABLED_POOLS ?= l4,a100,h100-high,h100-mega

bootstrap:
	PROJECT=$(PROJECT) REGION=$(REGION) bash scripts/bootstrap.sh
	python3 scripts/preflight.py --project $(PROJECT) --region $(REGION) --pools $(ENABLED_POOLS)
```

`lab/terraform/backend.tf`:
```hcl
terraform {
  backend "gcs" {}   # bucket/prefix supplied via -backend-config at init
  required_providers {
    google      = { source = "hashicorp/google",      version = ">= 5.30" }
    google-beta = { source = "hashicorp/google-beta", version = ">= 5.30" }
  }
}
```

- [ ] **Step 4: Verify** — `make bootstrap PROJECT=<p> REGION=us-central1` creates the bucket and prints a quota table (exit 0 for default pools if quota exists).

- [ ] **Step 5: Commit**
```bash
git add lab/Makefile lab/scripts/bootstrap.sh lab/scripts/preflight.py lab/terraform/backend.tf lab/env/_example.tfvars
git commit -m "feat(lab): SP-0 scaffolding, bootstrap and preflight"
```

---

### Task 2: Network + base cluster + system pool (Cluster Toolkit blueprint)

**Files:**
- Create: `lab/blueprints/base-cluster.yaml`, `lab/terraform/variables.tf`, `lab/env/us-central1.tfvars`
- Test: `gcluster` validate + `terraform validate`/`plan`

**Interfaces:**
- Consumes: `PROJECT`, `REGION`, `ENABLED_POOLS` (Task 1).
- Produces: a GKE cluster named `${deployment_name}` with a system CPU pool; VPC outputs (`network_self_link`, GPU/RDMA net links) consumed by Task 4; `enabled_pools` tfvar.

- [ ] **Step 1: Write the base blueprint** (`lab/blueprints/base-cluster.yaml`) — system VPC, GKE cluster, system pool. GPU VPCs are added conditionally in Task 3/4.
```yaml
blueprint_name: hpc-lab-sp0
vars:
  project_id: $(vars.project_id)
  region:     $(vars.region)
  deployment_name: hpc-lab
deployment_groups:
- group: primary
  modules:
  - id: system-net
    source: modules/network/vpc
  - id: gke-cluster
    source: modules/scheduler/gke-cluster
    use: [system-net]
    settings:
      enable_private_endpoint: false
      release_channel: REGULAR
      # workload identity on by default in this module
  - id: system-pool
    source: modules/compute/gke-node-pool
    use: [gke-cluster]
    settings:
      name: system
      machine_type: e2-standard-8
      autoscaling_total_min_nodes: 2
      autoscaling_total_max_nodes: 3
```
> Verify module source paths/field names against the pinned Cluster Toolkit version (`gcluster --version`); adjust names if the version differs. Do not invent fields — run `gcluster create --help` and the module READMEs.

- [ ] **Step 2: Write `variables.tf` + first env**

`lab/terraform/variables.tf`:
```hcl
variable "project_id" { type = string }
variable "region"     { type = string }
variable "enabled_pools" { type = list(string) }
variable "teams" { type = list(string), default = ["team-a"] }
variable "max_run_duration_seconds" { type = number, default = 86400 }
```
`lab/env/us-central1.tfvars`:
```hcl
project_id    = "REPLACE_WITH_PROJECT"   # set by operator
region        = "us-central1"
enabled_pools = ["l4", "a100", "h100-high", "h100-mega"]
teams         = ["team-a"]
```

- [ ] **Step 3: Add `make up` (validate+plan first)** to `lab/Makefile`:
```make
render:
	gcluster create blueprints/base-cluster.yaml --vars project_id=$(PROJECT),region=$(REGION) -o build/
plan: render
	cd build/hpc-lab/primary && terraform init -backend-config="bucket=$(PROJECT)-lab-tfstate" -backend-config="prefix=$(REGION)" && terraform validate && terraform plan -var-file=../../../env/$(REGION).tfvars
up: plan
	cd build/hpc-lab/primary && terraform apply -auto-approve -var-file=../../../env/$(REGION).tfvars
```

- [ ] **Step 4: Verify (GPU-free)** — `make plan` succeeds: `terraform validate` passes and `plan` shows the VPC, cluster, and system pool to be created. Optionally `make up` then `kubectl get nodes` shows system nodes `Ready`.
Expected: validate OK; plan non-empty, no errors.

- [ ] **Step 5: Commit**
```bash
git add lab/blueprints/base-cluster.yaml lab/terraform/variables.tf lab/env/us-central1.tfvars lab/Makefile
git commit -m "feat(lab): base network, GKE cluster and system pool blueprint"
```

---

### Task 3: GPU VPCs (TCPX/TCPXO/RDMA), gated by enabled pools

**Files:**
- Modify: `lab/blueprints/base-cluster.yaml`
- Test: `terraform plan` network-count assertion

**Interfaces:**
- Produces: up to 8 `gpu-net-N` VPC self-links (for TCPX/TCPXO) and one `rdma-net` (mrdma profile) consumed by Task 4 pool attachments. Created only if the corresponding pool is in `enabled_pools`.

- [ ] **Step 1: Add conditional GPU networks** to the blueprint. TCPX/TCPXO use additional VPCs; RDMA uses the RDMA network profile.
```yaml
  # 8 GPU networks (mega uses all 8, high uses first 4). Toolkit provides a
  # multi-vNIC helper; if unavailable in your version, instantiate vpc x8.
  - id: gpu-nets
    source: modules/network/multivpc
    settings:
      network_name_prefix: gpu-net
      global_ip_address_range: 192.168.0.0/16
      network_count: 8
      subnetwork_cidr_suffix: 24
  # RDMA network for a3-ultra / a4 (only referenced when those pools enabled)
  - id: rdma-net
    source: modules/network/rdma-vpc          # verify exact source name for your version
    settings: { network_profile: "mrdma" }
```
> If your Toolkit version names these differently, use the a3-mega / a3-ultra example blueprints as the source of truth for the exact module + settings. Keep `network_count` at 8; `h100-high` attaches only 4.

- [ ] **Step 2: Verify (GPU-free)** — `make plan` with `enabled_pools=["l4"]` shows **no** GPU/RDMA VPCs; with `["h100-mega"]` shows 8 GPU nets; with `["h200-ultra"]` shows the RDMA net.
Expected: network resources in the plan match the enabled set.

- [ ] **Step 3: Commit**
```bash
git add lab/blueprints/base-cluster.yaml
git commit -m "feat(lab): conditional GPU/RDMA VPCs for TCPX/TCPXO/RDMA fabrics"
```

---

### Task 4: GPU node-pool matrix (DWS, zero-scaled, labeled, fabric-attached)

**Files:**
- Create: `lab/blueprints/pools/{l4,a100,h100-high,h100-mega,h200-ultra,b200}.yaml`
- Modify: `lab/blueprints/base-cluster.yaml` (include enabled pool fragments)
- Test: `terraform plan` + node-pool describe assertions

**Interfaces:**
- Consumes: cluster (Task 2), GPU/RDMA nets (Task 3).
- Produces: node pools with `queuedProvisioning.enabled=true`, autoscale 0→N (min 0), labels `lab.gpu/type`,`lab.gpu/interconnect`, DWS taint.

- [ ] **Step 1: Write one pool fragment per type.** Example `pools/h100-mega.yaml`:
```yaml
- id: pool-h100-mega
  source: modules/compute/gke-node-pool
  use: [gke-cluster, gpu-nets]
  settings:
    name: h100-mega
    machine_type: a3-megagpu-8g
    autoscaling_total_min_nodes: 0
    autoscaling_total_max_nodes: 2
    initial_node_count: 0
    enable_queued_provisioning: true
    reservation_affinity: { consume_reservation_type: NO_RESERVATION }
    guest_accelerator:
    - { type: nvidia-h100-mega-80gb, count: 8, gpu_driver_version: DEFAULT }
    labels: { "lab.gpu/type": "h100-mega", "lab.gpu/interconnect": "tcpxo" }
    additional_networks: $(gpu-nets.additional_networks)   # attaches 8 GPU NICs
```
Fragments differ per type:
- `l4`: `g2-standard-96`, `nvidia-l4`, no `additional_networks`, interconnect `none`.
- `a100`: `a2-ultragpu-8g`, `nvidia-a100-80gb`, no additional nets, interconnect `nvswitch`.
- `h100-high`: `a3-highgpu-8g`, `nvidia-h100-80gb`, first **4** GPU nets, interconnect `tcpx`.
- `h100-mega`: as above (8 nets, `tcpxo`).
- `h200-ultra`: `a3-ultragpu-8g`, `nvidia-h200-141gb`, `use: [rdma-net]`, interconnect `rdma`.
- `b200`: `a4-highgpu-8g`, `nvidia-b200`, `use: [rdma-net]`, interconnect `rdma`.

- [ ] **Step 2: Include only enabled fragments.** Drive inclusion from `enabled_pools` (Toolkit: compose the blueprint via a small pre-render step in `make render` that concatenates enabled `pools/*.yaml` into the group; keep disabled ones out).

- [ ] **Step 3: Verify (GPU-free)** — `make plan ENABLED_POOLS=l4,h100-mega` shows exactly those two pools, each `min=0`, queued provisioning true, correct accelerator + labels. Optional `make up` then:
`gcloud container node-pools describe h100-mega --cluster hpc-lab --region $REGION --format="value(config.labels,autoscaling.minNodeCount,queuedProvisioning.enabled)"`
Expected: `min=0`, `queuedProvisioning.enabled=true`, `lab.gpu/type=h100-mega`.

- [ ] **Step 4: Commit**
```bash
git add lab/blueprints/pools lab/blueprints/base-cluster.yaml lab/Makefile
git commit -m "feat(lab): DWS zero-scaled GPU node-pool matrix with per-type fabric"
```

---

### Task 5: DCGM + GPUDirect/RDMA installer hooks (cluster-wide)

**Files:**
- Create: `lab/manifests/dcgm/dcgm-exporter.yaml`, `lab/terraform/modules/dcgm/main.tf`
- Test: `kubeconform` + `kubectl apply --dry-run=server`

**Interfaces:**
- Produces: DCGM exporter DaemonSet (node-selected to GPU pools) + the Toolkit-provided GPUDirect/RDMA installer daemonsets applied at cluster bring-up; a Terraform module that applies these manifests post-cluster.

- [ ] **Step 1: Add DCGM exporter manifest** (`lab/manifests/dcgm/dcgm-exporter.yaml`) targeting GPU nodes via `nodeSelector: {cloud.google.com/gke-accelerator: exists}` and tolerating the GPU/DWS taints. (Use the standard `nvcr.io/nvidia/k8s/dcgm-exporter` DaemonSet; pin the tag.)

- [ ] **Step 2: Verify (GPU-free)** — `kubeconform -strict lab/manifests/dcgm/dcgm-exporter.yaml` passes; `kubectl apply --dry-run=server -f` on the live cluster validates against the API.
Expected: no schema errors.

- [ ] **Step 3: Wire the GPUDirect/RDMA installer** — reference the Toolkit's per-fabric installer daemonset (TCPX/TCPXO/RDMA NCCL plugin + device injector) so provisioned nodes come up fabric-ready. Confirm the exact manifest URLs against the Toolkit A3/A4 examples; apply via the dcgm module.

- [ ] **Step 4: Commit**
```bash
git add lab/manifests/dcgm lab/terraform/modules/dcgm
git commit -m "feat(lab): DCGM exporter and GPUDirect/RDMA installer hooks"
```

---

### Task 6: Kueue install + flavors/queues + DWS ProvisioningRequest admission

**Files:**
- Create: `lab/manifests/kueue/{flavors,cluster-queues,local-queues,provisioning-config,namespaces}.yaml`, `lab/terraform/modules/kueue/main.tf`
- Test: `kubeconform` + Kueue dry-run admission

**Interfaces:**
- Consumes: pool labels (Task 4).
- Produces: one `ResourceFlavor` per GPU type (nodeLabels → `lab.gpu/type`); one `ClusterQueue` per type in a shared cohort; `LocalQueue` per team namespace; a `ProvisioningRequestConfig` + admission check wiring DWS.

- [ ] **Step 1: Install Kueue** via the kueue module (pin version), then add:

`provisioning-config.yaml`:
```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ProvisioningRequestConfig
metadata: { name: dws-flex }
spec:
  provisioningClassName: queued-provisioning.gke.io
  managedResources: ["nvidia.com/gpu"]
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: AdmissionCheck
metadata: { name: dws-flex }
spec:
  controllerName: kueue.x-k8s.io/provisioning-request
  parameters: { apiGroup: kueue.x-k8s.io, kind: ProvisioningRequestConfig, name: dws-flex }
```

`flavors` + `cluster-queues.yaml` (example for h100-mega):
```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata: { name: h100-mega }
spec: { nodeLabels: { "lab.gpu/type": "h100-mega" } }
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata: { name: cq-h100-mega }
spec:
  cohort: gpu
  admissionChecks: ["dws-flex"]
  resourceGroups:
  - coveredResources: ["nvidia.com/gpu","cpu","memory"]
    flavors:
    - name: h100-mega
      resources:
      - { name: "nvidia.com/gpu", nominalQuota: 16 }
      - { name: "cpu", nominalQuota: 400 }
      - { name: "memory", nominalQuota: 3000Gi }
```

- [ ] **Step 2: Team namespaces + LocalQueues** — for each `teams` entry create a namespace and a LocalQueue per GPU type.

- [ ] **Step 3: Verify (GPU-free)** — `kubeconform` passes; on the cluster `kubectl get clusterqueues,resourceflavors,admissionchecks` lists all; submit a **suspended** Kueue Job and confirm it is admitted-then-waiting-on-provisioning (no GPU spend).
Expected: admission check `dws-flex` present and `Active`.

- [ ] **Step 4: Commit**
```bash
git add lab/manifests/kueue lab/terraform/modules/kueue
git commit -m "feat(lab): Kueue with per-type queues and DWS provisioning admission"
```

---

### Task 7: Cost guardrails + teardown + zero-GPU assertion

**Files:**
- Create: `lab/terraform/modules/cost/main.tf`, `lab/scripts/assert_zero_gpu.py`
- Modify: `lab/Makefile` (`down`, `verify-cost`)
- Test: assertion script + `make down`

**Interfaces:**
- Produces: budget alert + GKE cost allocation; `make down [DESTROY=1]`; `assert_zero_gpu.py` (exit non-zero if any GPU VM or live ProvisioningRequest exists).

- [ ] **Step 1: Write the assertion** (`lab/scripts/assert_zero_gpu.py`):
```python
#!/usr/bin/env python3
import subprocess, sys, json
def sh(c): return subprocess.check_output(c, shell=True).decode()
proj = sys.argv[1]
vms = json.loads(sh(f"gcloud compute instances list --project {proj} "
  f"--filter='machineType~(a2|a3|a4|g2)' --format=json"))
gpu_vms = [v['name'] for v in vms]
prs = sh(f"kubectl get provisioningrequests -A --no-headers 2>/dev/null || true").strip()
if gpu_vms or prs:
    print(f"[FAIL] GPU VMs={gpu_vms} PRs present={bool(prs)}"); sys.exit(1)
print("[OK] zero GPU nodes, zero ProvisioningRequests"); sys.exit(0)
```

- [ ] **Step 2: Add teardown targets** to `lab/Makefile`:
```make
down:
	@if [ "$(DESTROY)" = "1" ]; then \
	  cd build/hpc-lab/primary && terraform destroy -auto-approve -var-file=../../../env/$(REGION).tfvars; \
	else \
	  kubectl delete provisioningrequests --all -A --ignore-not-found; \
	  for p in $$(echo $(ENABLED_POOLS) | tr ',' ' '); do \
	    gcloud container clusters resize hpc-lab --node-pool $$p --region $(REGION) --num-nodes 0 --quiet || true; done; \
	fi
verify-cost:
	python3 scripts/assert_zero_gpu.py $(PROJECT)
```

- [ ] **Step 3: Verify** — `make down && make verify-cost` prints `[OK] zero GPU nodes...` and exits 0.
Expected: exit 0.

- [ ] **Step 4: Commit**
```bash
git add lab/terraform/modules/cost lab/scripts/assert_zero_gpu.py lab/Makefile
git commit -m "feat(lab): cost guardrails, teardown tiers and zero-GPU invariant"
```

---

### Task 8: Smoke test (`make smoke GPU=<type>`)

**Files:**
- Create: `lab/manifests/smoke/nccl-smoke.yaml` (Kueue Job template), modify `lab/Makefile`
- Test: on-demand, per enabled GPU type (GPU spend — explicit)

**Interfaces:**
- Consumes: LocalQueue + flavor (Task 6), pools (Task 4).
- Produces: `make smoke GPU=<type>` that DWS-provisions 1 node via Kueue, runs `dcgmi diag -r 1` + an 8-GPU NCCL all-reduce, then releases.

- [ ] **Step 1: Write the smoke Job** — a Kueue-queued Job (`kueue.x-k8s.io/queue-name` label) targeting the type's LocalQueue, requesting `nvidia.com/gpu: 8`, running `dcgmi diag -r 1 && all_reduce_perf -b 512M -e 2G -g 8` (nccl-tests image). `backoffLimit: 0`, TTL after finish.

- [ ] **Step 2: Add target**:
```make
smoke:
	kubectl create -f manifests/smoke/nccl-smoke.yaml --dry-run=client -o yaml | sed 's/__GPU__/$(GPU)/g' | kubectl apply -f -
	kubectl wait --for=condition=complete job/nccl-smoke-$(GPU) --timeout=45m
	kubectl logs job/nccl-smoke-$(GPU) | tail -30
	kubectl delete job nccl-smoke-$(GPU) --ignore-not-found
```

- [ ] **Step 3: Verify (GPU spend, on demand)** — `make smoke GPU=l4` (cheapest) completes: DCGM diag pass + non-zero busbw, then node scales back to 0; `make verify-cost` returns OK.
Expected: job `complete`; logs show diag pass and all-reduce busbw.

- [ ] **Step 4: Commit**
```bash
git add lab/manifests/smoke lab/Makefile
git commit -m "feat(lab): on-demand NCCL/DCGM smoke test via Kueue DWS"
```

---

### Task 9: README howto + `VERIFY-SP0.md` acceptance checklist

**Files:**
- Create: `lab/README.md`, `lab/VERIFY-SP0.md`
- Test: `kubeconform`/markdown lint; the checklist is the human gate

**Interfaces:**
- Produces: operator howto and the user acceptance checklist (spec §8).

- [ ] **Step 1: Write `lab/VERIFY-SP0.md`** — copy the 8 acceptance steps from spec §8 verbatim as a runnable checklist with commands + pass criteria (cluster up; pools zero-scaled; Kueue healthy; DWS-via-Kueue provisions without reclaim; node fabric-ready `dcgmi diag`; NCCL sanity; DCGM metrics scraped; teardown + zero-cost invariant).

- [ ] **Step 2: Write `lab/README.md`** — prerequisites (APIs, quota bumps incl. VPCs ≥10, `gcluster` install, state bucket); the `make bootstrap/up/smoke/down` flow; a per-GPU-type example; a "what each VPC is for" diagram; troubleshooting linking `bugfixes/0001` (capacity waits) and `bugfixes/0002` (reclaim, and why Kueue supersedes the manual holder); day-2 (add a region/pool via new tfvars).

- [ ] **Step 3: Verify** — a reader can follow README end-to-end; run through `VERIFY-SP0.md` on the live cluster and confirm every step passes.
Expected: all checklist steps pass; user confirms.

- [ ] **Step 4: Commit**
```bash
git add lab/README.md lab/VERIFY-SP0.md
git commit -m "docs(lab): SP-0 README howto and user acceptance checklist"
```

---

## Self-Review

**Spec coverage:** §1 scope → Tasks 1–9; §2 tooling/layout → Tasks 1–2 + File Structure; §3 topology → Tasks 2,4; §4 networking → Tasks 3,4; §5 DWS+Kueue+cost → Tasks 6,7; §6 bootstrap/README → Tasks 1,9; §7 testing → per-task GPU-free checks + Task 8; §8 acceptance → Task 9 (`VERIFY-SP0.md`). No uncovered sections.

**Placeholder scan:** Code/commands are concrete. Two deliberate "verify against your Cluster Toolkit version" notes remain (Tasks 2,3,5) — these are *not* lazy placeholders; exact Toolkit module field names are version-specific and must be confirmed against the installed `gcluster`, with the command to do so given. `env/us-central1.tfvars` has `REPLACE_WITH_PROJECT` (an operator input, not a design gap).

**Type/name consistency:** pool ids (`l4,a100,h100-high,h100-mega,h200-ultra,b200`), labels (`lab.gpu/type`,`lab.gpu/interconnect`), flavor/queue names (`h100-mega`→`cq-h100-mega`), and `enabled_pools` are used consistently across Tasks 2–9. `make` targets (`bootstrap/render/plan/up/down/verify-cost/smoke`) are defined before use.

**Known execution risks (documented, not blockers):** exact Cluster Toolkit module names for `multivpc`/`rdma-vpc` vary by version; H200/B200 quota+region must be secured before enabling those pools; smoke tests incur GPU spend and require DWS capacity.
