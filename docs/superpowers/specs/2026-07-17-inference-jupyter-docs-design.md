# Inference Endpoint + Jupyter Host + Technical Doc — Design Spec

- **Date:** 2026-07-17
- **Status:** Approved (design); pending spec review → implementation plan
- **Depends on:** the live GKE cluster `hypercomputer-a3-cluster` (us-central1) and its DWS A3/H100 node; SP-0 lab foundation (on `main`)
- **Execution:** **live deploy now** on the current A3 node (gap-free handoff from the holder), plus durable artifacts + doc

## Goal

On the current cluster, stand up (1) an **internal inference endpoint** serving **Qwen3-32B** via vLLM, and (2) a **JupyterHub** notebook host with GPU access, then produce (3) a **technical document** (Markdown + Google Docs export) covering the entire stack — written for a technical audience **not** familiar with GPUs or Google AI infra.

## Decisions (from brainstorming)

| Dimension | Decision |
|-----------|----------|
| Model | **Qwen3-32B** (dense, ~64 GB fp16) |
| Serving stack | **vLLM** (OpenAI-compatible API), tensor-parallel = 2 |
| Serve target | the **existing A3 node** (8× H100 80GB), DWS Flex-Start |
| Endpoint exposure | **internal LoadBalancer** (VPC-only) |
| Notebooks | **JupyterHub on GKE** (Zero-to-JupyterHub), GPU notebook profiles |
| Execution | **deploy live now**; artifacts + doc are the durable deliverable |
| Doc | **Markdown in `docs/`** + **Google Docs export** |

## Sub-projects

- **SP-A — Inference serving** (vLLM + Qwen3-32B + internal LB).
- **SP-B — Jupyter host** (JupyterHub on GKE, GPU + CPU profiles).
- **SP-C — Technical documentation** (umbrella runbook; Markdown + Google Docs).

Build order A → B → C (doc documents what was actually deployed; capture real outputs/screens from A and B for it).

## 1. Architecture & GPU layout (single 8×H100 A3 node)

The A3 node `gke-...-16664d9c-hhp6` (us-central1-a) has 8× H100 80GB. Allocation:
- **vLLM: 2 GPUs** (tensor-parallel=2) — Qwen3-32B fits with KV-cache headroom.
- **Jupyter GPU notebooks: up to ~6 GPUs** — 1 H100 per GPU notebook pod, on demand.
- **Hub/proxy + system components: CPU** on the system pool.
- Both workloads carry the DWS tolerations (`nvidia.com/gpu`, `cloud.google.com/gke-queued`) and pin to the A3 node.
- While running, the workloads **occupy the node → it stays held** (house rule satisfied without a separate holder). The holder is re-armed on teardown.
- Namespaces: `inference`, `jupyter`.
- The node is the **7-day DWS one (expires ~2026-07-23)**: live services are a demo/showcase; manifests + doc are durable.

## 2. SP-A — Inference (vLLM + Qwen3-32B)

- **Deployment `qwen3-vllm`** (ns `inference`), image `vllm/vllm-openai:<pinned>`. Args:
  `--model Qwen/Qwen3-32B --tensor-parallel-size 2 --served-model-name qwen3-32b --gpu-memory-utilization 0.90 --max-model-len <ctx>`.
  Requests `nvidia.com/gpu: 2`, DWS tolerations, A3 nodeSelector.
- **Model weights & continuity:** Hugging Face download on first start (~65 GB) into a **PD-backed PVC** (`pd-ssd`) mounted as the HF cache — so pod restarts / node rotation don't re-download (see §6 continuity).
- **Endpoint:** `Service` type LoadBalancer, `networking.gke.io/load-balancer-type: "Internal"`, port 8000 → OpenAI-compatible `/v1/chat/completions` and `/v1/models`. Callable with any OpenAI client (`base_url` = internal IP).
- **Health:** readiness/liveness on `/health`.
- **Continuity option:** can run **2 replicas** (one per node) with a PodDisruptionBudget for zero-downtime node rotation (see §6).

## 3. SP-B — Jupyter host (JupyterHub on GKE)

- **Zero-to-JupyterHub** Helm chart (ns `jupyter`); hub + proxy on CPU (system pool).
- **Singleuser profiles:**
  - **GPU profile** — CUDA+PyTorch image, requests `nvidia.com/gpu: 1`, DWS tolerations, A3 nodeSelector.
  - **CPU profile** — no GPU, for light work.
- **Storage:** per-user **PD-backed PVC** home dirs (persist across node rotation).
- **Access:** internal LB (VPC). **Auth:** native/simple auth for the demo (documented); Google OAuth noted as the production option.
- **Example notebooks (baked in):** (a) `nvidia-smi` + `torch.cuda` GPU check; (b) **calling the vLLM endpoint** from a notebook (ties SP-A and SP-B together).

## 4. SP-C — Technical documentation

One umbrella runbook for a technical, non-GPU/GCP audience. Source Markdown in `docs/`, exported to Google Docs via the repo's existing HTML exporters.

1. **Intro + glossary** — plain-language primers: GPU / CUDA / H100 / NVLink-NVSwitch; GKE / node pool / pod / namespace; DWS Flex-Start; vLLM; JupyterHub; tensor parallelism.
2. **The infrastructure** — cluster → node pools → A3/H100 → **DWS Flex-Start** (why, the 7-day cap, holders, the reclaim-bug story) → networking. Documents the SP-0 work and the live cluster.
3. **Inference service** — vLLM/Qwen3-32B, deployment walkthrough, how to call the endpoint (curl + Python OpenAI client examples).
4. **Jupyter host** — logging in, launching a GPU notebook, running AI/ML, calling the model.
5. **Operations** — cost/holders, teardown, **node rotation before expiry** (see §6), capacity/reservation reality, troubleshooting (links to `bugfixes/`).
6. **Appendices** — exact commands, full manifests, architecture diagrams.

## 5. Deploy sequence & holder interplay

1. **Pre:** holder `a3-holder-zone-a` holds all 8 GPUs.
2. Apply vLLM + JupyterHub manifests → pods **Pending** (holder owns the GPUs).
3. **Scale holder to 0** → freed GPUs → vLLM (2) and hub bind; the running services now hold the node.
4. **Verify** (see §7); capture outputs/screens for the doc.
5. **Teardown (documented):** delete services → **re-arm holder** (`--replicas=1`) → node stays held until needed or expired.

## 6. Workload continuity across the 7-day node expiry

The DWS node is reclaimed at 7 days; continuity = surviving node loss and landing on a replacement:
- **State on persistent PD** (model cache, Jupyter homes) — network disks detach/reattach to the new node (same zone); no re-download, files intact. Never local SSD / `emptyDir` for durable state.
- **Controller-managed workloads** (Deployment / hub) — Kubernetes recreates the pod and the scheduler places it on any node with free GPUs. No manual pod moves.
- **Replacement node** — a pending GPU pod auto-triggers a new DWS ProvisioningRequest (Kueue-managed DWS, per SP-0), binding when capacity is granted. **Continuity, not zero-downtime** — a replacement H100 re-enters the DWS queue (capacity wait).
- **Zero-downtime path (overlap):** ~day 5–6 provision a *second* node while the old one runs; run vLLM at **2 replicas** (+ PodDisruptionBudget) so the internal LB serves from the healthy replica while the old node drains; then cordon/drain the old node. Overlap needs a second node's capacity (best-effort under H100 scarcity).
- **Durable fix:** move to the reservation-backed `h100-reserved` pool (no run-duration cap) once capacity is granted (account-rep path).
- A **"node rotation before expiry" runbook** is included in the doc (§4.5): pre-provision → overlap → drain → retire.

## 7. Testing / verification

- **vLLM:** `/health` ready; a real `curl` `/v1/chat/completions` returns Qwen3-32B output; `nvidia-smi` shows 2 GPUs in use; sample latency/throughput.
- **Jupyter:** log in; launch a GPU notebook; run `nvidia-smi` + `torch.cuda.is_available()`; call the vLLM endpoint from the notebook.
- **Continuity (documented, not necessarily executed):** verify PVCs are PD-backed (survive node loss); confirm Deployment recreates the pod when its node is drained.
- **Doc:** renders in Markdown; Google Docs export succeeds; glossary covers every GPU/GCP term used.

## Success criteria

An internal user, from inside the VPC, can (1) call the Qwen3-32B endpoint with an OpenAI client and get a completion, and (2) log into JupyterHub, launch a GPU notebook, and both run `torch.cuda` and call the model — and a non-GPU/GCP-familiar teammate can read the doc and understand the whole stack from cluster to endpoint. Durable state survives node rotation; teardown re-arms the holder.

## Open items to confirm at implementation time
- vLLM image tag + exact `--max-model-len` (context) for the KV-cache budget on 2× H100.
- Whether Qwen3-32B requires a Hugging Face token (gated?) — provide via a Secret if so.
- PD size for the model cache (≥ ~120 GB) and per-user Jupyter home size.
- Internal LB subnet/annotations for this VPC.

## Related
- `docs/superpowers/specs/2026-07-16-sp0-foundation-iac-design.md` (foundation; Kueue-managed DWS)
- `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md` (holders / reclaim)
- `lab/RESERVATIONS.md` (durable capacity path)
