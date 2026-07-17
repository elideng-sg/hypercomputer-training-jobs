# AI Hypercomputer Reproduction Lab — Program Overview

- **Date:** 2026-07-16
- **Status:** Approved (decomposition + approach)
- **Owner:** elideng
- **Scope:** Program-level framing. Each sub-project gets its own spec → plan → build cycle.

## Goal

Build a **lab environment on GCP AI Hypercomputer (GKE) that reproduces and debugs
production problems** across the full GPU stack — hardware, drivers, NCCL/networking
plugins, and distributed training workloads (PyTorch/TensorFlow/JAX) — so incidents
seen in production can be recreated, isolated, and understood in a controlled,
version-controlled environment.

## Requirements (from brainstorming)

| Dimension | Decision |
|-----------|----------|
| Production environment to mirror | **GKE on GCP** (1:1 fidelity) |
| Problem classes to reproduce | **All four:** GPU hardware & drivers; NCCL & networking; distributed training/frameworks; cluster & orchestration |
| GPU generations | **L4 (G2), A100 (A2), H100 (A3 High/Mega), H200 (A3 Ultra), B200 (A4)** |
| Cost / capacity model | **Ephemeral on-demand via DWS flex-start**; spin up per repro, tear down |
| Users | **Small team** → Kueue + per-team namespaces/quotas |
| Reproduction methods | **All four:** version/config matching; fault injection/chaos; artifact capture & replay; golden workloads + observability |

## Architecture approach (selected)

**Approach A — Declarative IaC (Cluster Toolkit / Terraform) + Kueue + modular add-ons.**
A reproduction lab must be deterministic and fidelity-accurate, which mandates
IaC. Cluster Toolkit is used specifically because it encodes the hard-to-get-right
GPUDirect-TCPX/TCPXO and RDMA/CX-7 networking for A3/A4. The existing imperative
bash scripts become reference material and feed the `bugfixes/` knowledge base.
Rejected: growing the bash scripts (non-reproducible, drift-prone — already bit us
twice) and fully-managed kits like XPK (abstract away the exact driver/NCCL/topology
knobs we need to debug).

## Delivery convention (all sub-projects)

Every sub-project (SP-0 through SP-6) must deliver, in addition to its features:
1. **Automated tests** — GPU-free static/CI checks plus on-demand smoke tests.
2. **A user-facing verification / acceptance checklist** — concrete, copy-pasteable
   steps the user runs and observes to *confirm the phase works*, each with an
   expected result / pass criterion. A phase is not "done" until the user has run
   this checklist and confirmed. This is distinct from automated tests: it is the
   human sign-off gate.

## Decomposition (7 sub-projects)

They stack; everything sits on SP-0.

| # | Sub-project | Purpose | Depends on |
|---|-------------|---------|-----------|
| SP-0 | **Foundation / IaC** | One region-parameterized GKE cluster; DWS zero-scaled GPU pool matrix with correct per-type networking; Kueue team quotas; cost/teardown; health gate. | — |
| SP-1 | **GPU & driver matrix** | Pinned driver/CUDA/NCCL/framework combos; image build pipeline (Artifact Registry); MIG; driver install modes; DCGM health. | SP-0 |
| SP-2 | **Networking & NCCL** | GPUDirect/RDMA plugin tuning per type; NCCL env matrix; `nccl-tests` golden benchmarks; topology validation. | SP-0, SP-1 |
| SP-3 | **Workloads** | PyTorch (DDP/FSDP), TF, JAX; single/multi-node via JobSet; golden reference jobs + a real small LLM (MaxText/NeMo); FP8. | SP-1, SP-2 |
| SP-4 | **Observability & diagnostics** | DCGM exporter + Managed Prometheus + Grafana; XID/ECC/NVLink/NCCL dashboards; log pipeline; lab-vs-prod comparison. | SP-0 |
| SP-5 | **Fault injection / chaos** | Controlled fault library: GPU reset/XID, NIC drop/latency, NCCL timeout, OOM, preemption, autoscaler churn. | SP-3, SP-4 |
| SP-6 | **Reproduction control plane** | Ingest a prod incident fingerprint (versions + topology + job spec + env), materialize matching lab config, run, capture, compare; incident/`bugfixes/` knowledge base. | all |

## First sub-project

**SP-0 (Foundation / IaC)** — see `2026-07-16-sp0-foundation-iac-design.md`.

## Related
- `bugfixes/0001-dws-zone-requests-not-zone-pinned.md`
- `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md`
