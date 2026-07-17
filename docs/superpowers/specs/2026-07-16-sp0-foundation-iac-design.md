# SP-0 — Foundation / IaC — Design Spec

- **Date:** 2026-07-16
- **Status:** Approved (design); pending spec review → implementation plan
- **Program:** AI Hypercomputer Reproduction Lab (see `2026-07-16-hypercomputer-lab-program-overview.md`)
- **Depends on:** none (base layer)

## 1. Scope & non-goals

### Delivers
A deterministic, version-controlled foundation that later sub-projects plug into:
- One **region-parameterized** regional GKE cluster (GPU availability varies by region).
- A **matrix of GPU node pools** — L4, A100, H100 (A3 High + Mega), H200 (A3 Ultra),
  B200 (A4) — every one **DWS flex-start, autoscaled 0→N, defaulting to 0**.
- **Correct networking per GPU type baked in:** gVNIC (L4/A100), GPUDirect-TCPX/TCPXO
  (A3), RDMA/CX-7 (A3-Ultra/A4).
- **Kueue** with per-team namespaces/quotas and DWS ProvisioningRequest admission.
- Cost guardrails + one-command teardown + a **"foundation healthy" smoke gate**.
- Cluster-wide hooks so pools come up fabric-ready: GPUDirect/RDMA installer
  daemonsets and DCGM installed here (tuning/benchmarking deferred).

### Non-goals (later sub-projects)
Driver/CUDA/NCCL version *matrix* & image builds (SP-1); NCCL tuning & `nccl-tests`
benchmarking (SP-2); training workloads (SP-3); dashboards (SP-4); fault injection
(SP-5); reproduction control plane (SP-6).

### Success criteria
From a clean project, following the README, an operator can `make up` a region,
DWS-provision **any enabled GPU type** through Kueue and have it come up
**fabric-ready** (GPUDirect/RDMA working), pass the smoke gate, and `make down` back
to ~zero GPU cost — reproducibly from version-controlled config.

## 2. IaC tooling & repo layout

**Tooling:** **Cluster Toolkit** (`gcluster`) blueprints generate the Terraform for the
cluster, GPU pools, and GPUDirect/RDMA networking (it encodes the hard-to-get-right
A3/A4 fabric). Our own Terraform modules wrap Kueue, cost, and IAM. **GCS remote state
backend.** Chosen over hand-rolled Terraform because TCPXO (8 VPCs) and RDMA/CX-7 are
exactly where hand-rolling goes subtly wrong, and fidelity is the point of the lab.

**Repo home:** in this repo under `lab/` (shared history; `bugfixes/` lives alongside).

```
lab/
├── blueprints/           # Cluster Toolkit blueprints (cluster + per-GPU pools + fabric)
│   ├── base-cluster.yaml
│   └── pools/{l4,a100,h100-high,h100-mega,h200-ultra,b200-a4}.yaml
├── terraform/            # generated + wrapper modules (kueue, cost, iam); GCS backend
├── manifests/            # cluster-wide: Kueue CRDs/queues, DCGM, namespaces, installers
├── env/                  # per-region tfvars (project, region, enabled pools, quotas, teams)
└── Makefile              # bootstrap / up / down / smoke wrappers
```
The existing bash scripts remain as reference and feed `bugfixes/`.

## 3. Cluster & node-pool topology

GPU types are not all available in one region (L4/A100/H100 broad; H200/B200 scarce,
few regions). SP-0 is therefore a **module deployed once per region**, each cluster
enabling only the pools available there, gated by `env/<region>.tfvars` flags.

Per-cluster shape:
- **System pool:** small `e2`/`n2` CPU pool (2–3 nodes) for GKE system, Kueue, DCGM,
  DWS admission. Cheap, always-on.
- **GPU pools** (all DWS flex-start, autoscale 0→N, default 0):

| Pool | Machine type | GPU | Interconnect | Node networking |
|------|-------------|-----|--------------|-----------------|
| `l4` | `g2-standard-96` | 8× L4 | none | single gVNIC |
| `a100` | `a2-ultragpu-8g` | 8× A100 80GB | NVSwitch | gVNIC |
| `h100-high` | `a3-highgpu-8g` | 8× H100 | NVSwitch + GPUDirect-TCPX | system NIC + 4 GPU NICs |
| `h100-mega` | `a3-megagpu-8g` | 8× H100 | NVSwitch + GPUDirect-TCPXO | system NIC + 8 GPU NICs |
| `h200-ultra` | `a3-ultragpu-8g` | 8× H200 | NVSwitch + GPUDirect-RDMA (CX-7) | system NIC + RDMA NICs |
| `b200` | `a4-highgpu-8g` | 8× B200 | NVSwitch + RDMA (CX-7) | system NIC + RDMA NICs |

Every pool carries consistent labels/taints (`lab.gpu/type`, `lab.gpu/interconnect`)
so Kueue ResourceFlavors and later SPs target them deterministically.

## 4. Networking / VPC design

- **System VPC** (1): cluster control, CPU pool, L4/A100 pools, k8s/DWS traffic.
- **GPU VPCs for TCPX/TCPXO** (up to 8): `a3-high` uses the first 4, `a3-mega` all 8 —
  each a dedicated VPC+subnet attached as an additional node network; Toolkit installs
  the GPUDirect-TCPX(O) NCCL plugin + device injector daemonsets.
- **RDMA VPC** (1): dedicated network using the **RDMA network profile** (`mrdma`) for
  `a3-ultra`/`a4`; GPUDirect-RDMA binaries via the Toolkit RDMA installer.

Design rules:
- **VPC quota:** ~10 networks exceeds the default per-project limit (5); SP-0 documents
  and preflights the quota bump.
- GPU/RDMA VPCs are created **only** when their pools are enabled for the region.
- Firewall: intra-cluster + GPU-fabric allow rules scoped by network tags; no public
  GPU exposure.

## 5. DWS + Kueue + cost model

**DWS via Kueue (not the manual holder).** SP-0 uses **Kueue's ProvisioningRequest
admission check**: a job in a LocalQueue triggers Kueue to auto-create the DWS
ProvisioningRequest, admit pods when `Provisioned`, and clean up. This supersedes the
manual zone-pinned request + consumer-holder pattern from `bugfixes/0002`, which stays
documented as the manual fallback and the "why Kueue does it better" reference (Kueue
owns the booking lifecycle → no reclaim race).

- **Kueue topology:** ResourceFlavors keyed to pool `lab.gpu/type` labels; one
  ClusterQueue per GPU type in a shared cohort; LocalQueues per team namespace with
  quotas.
- **Cost model (ephemeral-first):** GPU pools autoscale `0→N`, default **0**;
  `maxRunDuration` configurable, default **24h**; GKE cost allocation on; budget alerts;
  invariant: *no GPU node runs without an admitted Kueue workload*.
- **Teardown tiers:** `scale-to-zero` (drain pools, delete PRs, keep cluster) and
  `destroy` (full `terraform destroy`). Job TTL cleans finished workloads.

## 6. Bootstrap, day-2 ops & README howto

**Prerequisites (documented + partly scripted):** project + billing; enable APIs;
**quota bumps** (VPCs ≥10, per-GPU quotas, DWS); `gcluster` install; GCS state bucket.

**Operator flow (`Makefile` wrappers):**
- `make bootstrap` — state bucket, APIs, preflight quota check.
- `make up REGION=us-central1` — render blueprint → `terraform apply` (cluster + enabled
  pools + VPCs + Kueue + DCGM).
- `make smoke GPU=h100-high` — DWS-provision 1 node via a Kueue job, run DCGM + a tiny
  NCCL all-reduce, release. The **foundation-healthy gate**.
- `make down [DESTROY=1]` — scale-to-zero or full destroy.

**README howto (explicit deliverable):** end-to-end walkthrough; a per-GPU-type example
per pool; quota prerequisites; a "what each VPC is for" diagram; troubleshooting that
links to `bugfixes/` (capacity waits → 0001, reclaim → 0002). **Day-2:** add a GPU type
or region (re-deploy module with different tfvars); upgrade notes.

## 7. Testing & success criteria

**Testing (no perpetual GPU spend):**
- **Static/CI (GPU-free):** `terraform validate` + `plan`; blueprint lint; `kubeconform`
  on manifests; Kueue dry-run admission.
- **Idempotency:** re-`apply` is a no-op (drift guard — the failure mode we hit twice).
- **Per-GPU smoke (on demand):** provision 1 node → `dcgmi diag -r 1` → 2-GPU + 8-GPU
  NCCL all-reduce sanity → release. A light 2-node all-reduce validates TCPX/RDMA where
  the type warrants; deep benchmarking is SP-2.
- **Teardown assertion:** after `make down`, verify zero GPU nodes and zero live
  ProvisioningRequests (cost invariant).

**Success criteria:** as in §1 — reproducible `make up` → DWS-provision any enabled GPU
type through Kueue, fabric-ready, smoke passes, `make down` to ~zero GPU cost.

## 8. User acceptance / verification steps

Delivered as a `lab/VERIFY-SP0.md` checklist (and summarized in the README). The user
runs these after the build to confirm SP-0 works; SP-0 is not "done" until every step
passes and the user confirms. Each step lists the command and its pass criterion.

1. **Cluster is up** — `gcloud container clusters describe <name> --region <r>` → `RUNNING`;
   `kubectl get nodes` shows the system pool `Ready`.
2. **GPU pools exist and are zero-scaled** — `gcloud container node-pools list` shows every
   enabled pool; each GPU pool reports current size **0** (no idle GPU cost at rest).
3. **Kueue is healthy** — `kubectl get clusterqueues,localqueues,resourceflavors` shows the
   per-GPU-type flavors/queues; Kueue pods `Ready`.
4. **DWS provisioning works via Kueue** — submit the smoke job for a GPU type; observe the
   Kueue-created ProvisioningRequest reach `Provisioned=True`, the pod schedule, and — the
   guard against `bugfixes/0002` — the node is **not** reclaimed while the job runs.
5. **Node comes up fabric-ready** — on the provisioned node: `dcgmi diag -r 1` passes;
   the GPUDirect/RDMA plugin daemonset pods are `Running`; expected NICs present
   (4 for `h100-high`, 8 for `h100-mega`, RDMA/`mrdma` for `h200-ultra`/`b200`).
6. **NCCL sanity** — the smoke job's 2-GPU and 8-GPU all-reduce complete without error
   (non-zero busbw); for fabric types, the light 2-node all-reduce completes.
7. **Observability hooks present** — DCGM metrics are being scraped (target/metric visible),
   confirming SP-4 can build on them.
8. **Teardown & cost invariant** — `make down` (and `DESTROY=1` variant) leaves **zero GPU
   nodes** and **zero live ProvisioningRequests**; `gcloud compute instances list` shows no
   GPU VMs. Re-running `make up` (idempotency) is a no-op plan.

Per-GPU-type steps are only expected for pools enabled in the target region.

## Open items / prerequisites to confirm at implementation time
- Target region(s) for first deployment and which pools each enables (capacity/quota).
- Per-GPU quota availability in the chosen project.
- Team list + namespace/quota split for Kueue.

## Related
- Program overview: `2026-07-16-hypercomputer-lab-program-overview.md`
- `bugfixes/0001-dws-zone-requests-not-zone-pinned.md`, `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md`
