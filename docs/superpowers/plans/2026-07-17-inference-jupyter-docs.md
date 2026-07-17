# Qwen3-32B Inference + JupyterHub + Technical Doc — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live-deploy a Qwen3-32B vLLM inference endpoint (internal LB, OpenAI-compatible) and JupyterHub-on-GKE (GPU/CPU notebooks) on the current A3 8×H100 node, then write the internal technical doc — with workload state on persistent disk so it survives node rotation.

**Architecture:** Both services run on the held DWS A3 node (`gke-...-16664d9c-hhp6`, us-central1-a). Gap-free handoff: apply manifests → scale the holder to 0 → services bind the freed H100s and hold the node. vLLM uses 2 GPUs (TP=2); Jupyter GPU notebooks use 1 each. State (model cache, notebook homes) lives on PD-backed PVCs.

**Tech Stack:** GKE, vLLM (`vllm/vllm-openai`), Qwen3-32B, JupyterHub (Zero-to-JupyterHub Helm), kubectl, helm, gcloud, internal LoadBalancer.

## Global Constraints

- Deploy on the **existing A3 node**; both workloads tolerate `nvidia.com/gpu` and `cloud.google.com/gke-queued` and pin to it (nodeSelector `cloud.google.com/gke-accelerator: nvidia-h100-80gb` or the pool label). (spec §1)
- **vLLM: `--tensor-parallel-size 2`, `nvidia.com/gpu: 2`, `--max-model-len 32768`, `--gpu-memory-utilization 0.90`, `--served-model-name qwen3-32b`.** (spec §2)
- **Endpoint = internal LoadBalancer** via `networking.gke.io/load-balancer-type: "Internal"`, port 8000, OpenAI-compatible. (spec §2)
- **Durable state on PD-backed PVCs only** — model cache (`pd-ssd`, ≥150Gi) and per-user Jupyter homes; never local SSD/`emptyDir`. (spec §6)
- **Controller-managed workloads** (Deployment / hub) so K8s reschedules them on node rotation. (spec §6)
- **Gap-free handoff**: manifests applied before scaling the holder down; **re-arm the holder on teardown** (house rule). (spec §5)
- Namespaces `inference` and `jupyter`. (spec §1)
- The node is the **7-day DWS node (~2026-07-23)** — live services are a demo; manifests + doc are durable. (spec §1)

**Deploy-time values (confirm in Task 0):** `VLLM_IMAGE` (pin to current stable, e.g. `vllm/vllm-openai:v0.6.6`); whether `Qwen/Qwen3-32B` is HF-gated (add `hf-token` Secret if so); internal-LB subnet for the VPC.

---

## File Structure

```
deploy/
├── inference/
│   ├── namespace.yaml
│   ├── model-cache-pvc.yaml         # pd-ssd 150Gi HF cache
│   ├── vllm-deployment.yaml         # Qwen3-32B, TP=2, 2 GPU
│   ├── vllm-service-internal.yaml   # internal LB :8000
│   └── vllm-pdb.yaml                # for 2-replica overlap (continuity)
├── jupyter/
│   ├── values.yaml                  # Zero-to-JupyterHub helm values
│   └── examples/{gpu_check.ipynb,call_vllm.ipynb}
└── ops/
    ├── handoff.sh                   # gap-free: apply -> scale holder 0
    ├── rearm-holder.sh              # teardown -> holder replicas=1
    └── node-rotation-runbook.md     # overlap/drain/retire
docs/
├── AI_INFRA_RUNBOOK.md              # umbrella technical doc (SP-C)
└── (Google Docs export via existing scripts/export_to_google_docs*.py)
```

---

### Task 0: Tooling, cluster access & namespaces

**Files:** Create `deploy/inference/namespace.yaml`, `deploy/jupyter/` (dir); no app logic yet.

**Interfaces:**
- Produces: working `kubectl` + `helm` against the cluster; namespaces `inference`, `jupyter`; confirmed `VLLM_IMAGE` and HF-gating.

- [ ] **Step 1: Install kubectl, helm, gke-gcloud-auth-plugin (binaries to ~/.local/bin)**

```bash
mkdir -p ~/.local/bin
# kubectl
curl -fsSLo ~/.local/bin/kubectl "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && chmod +x ~/.local/bin/kubectl
# helm
curl -fsSL https://get.helm.sh/helm-v3.16.2-linux-amd64.tar.gz | tar xz -C /tmp && mv /tmp/linux-amd64/helm ~/.local/bin/helm
# gke auth plugin (binary)
gcloud components install gke-gcloud-auth-plugin --quiet 2>/dev/null || \
  curl -fsSLo ~/.local/bin/gke-gcloud-auth-plugin "https://storage.googleapis.com/gke-release/gke-gcloud-auth-plugin/$(uname -s | tr A-Z a-z)/amd64/gke-gcloud-auth-plugin" 2>/dev/null && chmod +x ~/.local/bin/gke-gcloud-auth-plugin || true
```

- [ ] **Step 2: Get cluster credentials & verify access**

```bash
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
gcloud container clusters get-credentials hypercomputer-a3-cluster --location=us-central1 --project=hdlab-elideng
kubectl get nodes -o wide
```
Expected: nodes list incl. the A3 node `gke-...-16664d9c-hhp6` Ready. If the auth plugin binary route fails, fall back to the REST approach used earlier and report BLOCKED.

- [ ] **Step 3: Confirm the A3 node label/taints, VLLM_IMAGE, and HF gating**

```bash
kubectl get node -l cloud.google.com/gke-accelerator=nvidia-h100-80gb -o jsonpath='{.items[0].metadata.name}{"\n"}'
kubectl describe node <a3-node> | grep -A3 Taints
# HF gating check (no auth): expect HTTP 200 if ungated
curl -sI https://huggingface.co/Qwen/Qwen3-32B/resolve/main/config.json | head -1
```
Record the exact node label to use as nodeSelector. If the HF check is 401/403, plan to add an `hf-token` Secret.

- [ ] **Step 4: Create namespaces**

`deploy/inference/namespace.yaml`:
```yaml
apiVersion: v1
kind: Namespace
metadata: { name: inference }
---
apiVersion: v1
kind: Namespace
metadata: { name: jupyter }
```
```bash
kubectl apply -f deploy/inference/namespace.yaml
kubectl get ns inference jupyter
```
Expected: both `Active`.

- [ ] **Step 5: Commit**

```bash
git add deploy/inference/namespace.yaml
git commit -m "chore(deploy): tooling, cluster access, and namespaces"
```

---

### Task 1: vLLM Qwen3-32B Deployment + internal endpoint (SP-A)

**Files:** Create `deploy/inference/model-cache-pvc.yaml`, `vllm-deployment.yaml`, `vllm-service-internal.yaml`.

**Interfaces:**
- Consumes: namespaces + node label (Task 0); the holder `a3-holder-zone-a` currently on the node.
- Produces: internal endpoint IP serving OpenAI-compatible `/v1/chat/completions` with model `qwen3-32b`.

- [ ] **Step 1: Model-cache PVC (PD-backed, survives node rotation)**

`deploy/inference/model-cache-pvc.yaml`:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: hf-cache, namespace: inference }
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: premium-rwo      # GKE pd-ssd dynamic class
  resources: { requests: { storage: 150Gi } }
```

- [ ] **Step 2: vLLM Deployment**

`deploy/inference/vllm-deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: qwen3-vllm, namespace: inference, labels: { app: qwen3-vllm } }
spec:
  replicas: 1
  selector: { matchLabels: { app: qwen3-vllm } }
  template:
    metadata: { labels: { app: qwen3-vllm } }
    spec:
      nodeSelector: { cloud.google.com/gke-accelerator: nvidia-h100-80gb }
      tolerations:
      - { key: "nvidia.com/gpu", operator: "Exists", effect: "NoSchedule" }
      - { key: "cloud.google.com/gke-queued", operator: "Exists", effect: "NoSchedule" }
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.6.6      # VLLM_IMAGE from Task 0
        args: ["--model","Qwen/Qwen3-32B","--served-model-name","qwen3-32b",
               "--tensor-parallel-size","2","--gpu-memory-utilization","0.90",
               "--max-model-len","32768","--host","0.0.0.0","--port","8000"]
        env:
        - { name: HF_HOME, value: /hf-cache }
        # - { name: HUGGING_FACE_HUB_TOKEN, valueFrom: { secretKeyRef: { name: hf-token, key: token } } }  # only if gated
        ports: [{ containerPort: 8000 }]
        resources:
          limits: { nvidia.com/gpu: "2", cpu: "24", memory: "200Gi" }
          requests: { nvidia.com/gpu: "2", cpu: "24", memory: "200Gi" }
        readinessProbe: { httpGet: { path: /health, port: 8000 }, initialDelaySeconds: 60, periodSeconds: 15, failureThreshold: 40 }
        livenessProbe: { httpGet: { path: /health, port: 8000 }, initialDelaySeconds: 600, periodSeconds: 30 }
        volumeMounts:
        - { name: hf-cache, mountPath: /hf-cache }
        - { name: shm, mountPath: /dev/shm }
      volumes:
      - { name: hf-cache, persistentVolumeClaim: { claimName: hf-cache } }
      - { name: shm, emptyDir: { medium: Memory, sizeLimit: 16Gi } }
```

- [ ] **Step 3: Internal LB Service**

`deploy/inference/vllm-service-internal.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: qwen3-vllm
  namespace: inference
  annotations: { networking.gke.io/load-balancer-type: "Internal" }
spec:
  type: LoadBalancer
  selector: { app: qwen3-vllm }
  ports: [{ port: 8000, targetPort: 8000, protocol: TCP }]
```

- [ ] **Step 4: Apply manifests, then gap-free handoff (scale holder to 0)**

```bash
kubectl apply -f deploy/inference/model-cache-pvc.yaml -f deploy/inference/vllm-deployment.yaml -f deploy/inference/vllm-service-internal.yaml
kubectl -n inference get pod -l app=qwen3-vllm     # Pending: holder owns the GPUs
kubectl -n default scale deploy/a3-holder-zone-a --replicas=0   # release GPUs -> vLLM binds
kubectl -n inference get pod -l app=qwen3-vllm -w   # wait until Running
```
Expected: pod schedules on the A3 node; model downloads to the PVC (several min); then `Running`.

- [ ] **Step 5: Verify the endpoint (real inference)**

```bash
kubectl -n inference wait --for=condition=ready pod -l app=qwen3-vllm --timeout=1200s
IP=$(kubectl -n inference get svc qwen3-vllm -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s "http://$IP:8000/v1/models"
curl -s "http://$IP:8000/v1/chat/completions" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"Say hello in one sentence."}],"max_tokens":40}'
kubectl -n inference exec deploy/qwen3-vllm -- nvidia-smi --query-gpu=index,memory.used --format=csv
```
Expected: `/v1/models` lists `qwen3-32b`; chat returns a completion; nvidia-smi shows 2 GPUs in use. Save this output for the doc.

- [ ] **Step 6: Commit**

```bash
git add deploy/inference/model-cache-pvc.yaml deploy/inference/vllm-deployment.yaml deploy/inference/vllm-service-internal.yaml
git commit -m "feat(inference): vLLM Qwen3-32B deployment + internal LB endpoint"
```

---

### Task 2: JupyterHub on GKE with GPU notebooks (SP-B)

**Files:** Create `deploy/jupyter/values.yaml`, `deploy/jupyter/examples/gpu_check.ipynb`, `deploy/jupyter/examples/call_vllm.ipynb`.

**Interfaces:**
- Consumes: namespaces + node label (Task 0); the vLLM Service DNS `qwen3-vllm.inference.svc.cluster.local:8000` (Task 1).
- Produces: JupyterHub reachable on an internal LB; a GPU notebook profile landing on the A3 node.

- [ ] **Step 1: Helm values**

`deploy/jupyter/values.yaml`:
```yaml
hub:
  config:
    JupyterHub: { authenticator_class: dummy }   # demo auth; OAuth for prod (documented)
    DummyAuthenticator: { password: "REPLACE_AT_DEPLOY" }
proxy:
  service: { type: LoadBalancer, annotations: { networking.gke.io/load-balancer-type: "Internal" } }
singleuser:
  storage: { dynamic: { storageClass: premium-rwo }, capacity: 20Gi }
  profileList:
  - display_name: "CPU (no GPU)"
    default: true
    kubespawner_override: { cpu_limit: 4, mem_limit: "16G" }
  - display_name: "GPU (1x H100)"
    kubespawner_override:
      image: quay.io/jupyter/pytorch-notebook:cuda12-latest
      extra_resource_limits: { "nvidia.com/gpu": "1" }
      node_selector: { cloud.google.com/gke-accelerator: nvidia-h100-80gb }
      tolerations:
      - { key: "nvidia.com/gpu", operator: "Exists", effect: "NoSchedule" }
      - { key: "cloud.google.com/gke-queued", operator: "Exists", effect: "NoSchedule" }
```

- [ ] **Step 2: Install JupyterHub via Helm**

```bash
helm repo add jupyterhub https://hub.jupyter.org/helm-chart/ && helm repo update
helm upgrade --install jhub jupyterhub/jupyterhub --namespace jupyter \
  --version 3.3.8 --values deploy/jupyter/values.yaml --timeout 15m
kubectl -n jupyter get pods
```
Expected: `hub` and `proxy` pods `Running`.

- [ ] **Step 3: Get the hub URL & log in**

```bash
kubectl -n jupyter get svc proxy-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```
Expected: internal IP; browse to `http://<ip>`, log in (dummy auth), select **GPU (1x H100)**.

- [ ] **Step 4: Verify a GPU notebook (real GPU + call the model)**

`deploy/jupyter/examples/gpu_check.ipynb` (cells): `!nvidia-smi` and:
```python
import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))
```
`deploy/jupyter/examples/call_vllm.ipynb`:
```python
from openai import OpenAI
c = OpenAI(base_url="http://qwen3-vllm.inference.svc.cluster.local:8000/v1", api_key="none")
print(c.chat.completions.create(model="qwen3-32b",
      messages=[{"role":"user","content":"2+2?"}], max_tokens=20).choices[0].message.content)
```
In a launched GPU notebook, run both. Expected: CUDA True + H100 name; the vLLM call returns a completion. Capture screenshots/output for the doc.

- [ ] **Step 5: Commit**

```bash
git add deploy/jupyter/values.yaml deploy/jupyter/examples
git commit -m "feat(jupyter): JupyterHub on GKE with GPU notebook profile + examples"
```

---

### Task 3: Continuity (2-replica overlap, PDB) + ops scripts

**Files:** Create `deploy/inference/vllm-pdb.yaml`, `deploy/ops/handoff.sh`, `deploy/ops/rearm-holder.sh`, `deploy/ops/node-rotation-runbook.md`.

**Interfaces:**
- Consumes: the vLLM Deployment (Task 1), the holder (default ns).
- Produces: documented + scripted node-rotation/teardown path.

- [ ] **Step 1: PodDisruptionBudget for vLLM**

`deploy/inference/vllm-pdb.yaml`:
```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: qwen3-vllm, namespace: inference }
spec: { minAvailable: 1, selector: { matchLabels: { app: qwen3-vllm } } }
```
```bash
kubectl apply -f deploy/inference/vllm-pdb.yaml
```

- [ ] **Step 2: Ops scripts (handoff + re-arm holder)**

`deploy/ops/handoff.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
kubectl apply -f deploy/inference/  # PVC, deployment, service, pdb
kubectl -n default scale deploy/a3-holder-zone-a --replicas=0
echo "[+] holder released; vLLM binding the A3 node"
```
`deploy/ops/rearm-holder.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
kubectl -n inference delete deploy qwen3-vllm --ignore-not-found
kubectl -n default scale deploy/a3-holder-zone-a --replicas=1
echo "[+] services torn down; holder re-armed (house rule)"
```
```bash
chmod +x deploy/ops/handoff.sh deploy/ops/rearm-holder.sh
bash -n deploy/ops/handoff.sh deploy/ops/rearm-holder.sh   # syntax check
```

- [ ] **Step 3: Node-rotation runbook**

`deploy/ops/node-rotation-runbook.md` — the overlap procedure: (1) ~day 5–6 submit a new DWS request for a 2nd node; (2) `kubectl -n inference scale deploy/qwen3-vllm --replicas=2` (one per node, PDB keeps 1 serving); (3) confirm both endpoints healthy; (4) `kubectl cordon <old-node>` then `kubectl drain <old-node> --ignore-daemonsets --delete-emptydir-data`; (5) let the old node expire; (6) scale back to 1. Note PVCs (PD) reattach same-zone; Kueue auto-reprovisions a pending pod's node (capacity permitting).

- [ ] **Step 4: Commit**

```bash
git add deploy/inference/vllm-pdb.yaml deploy/ops
git commit -m "feat(ops): vLLM PDB, gap-free handoff + re-arm scripts, node-rotation runbook"
```

---

### Task 4: Technical documentation (SP-C)

**Files:** Create `docs/AI_INFRA_RUNBOOK.md`; produce a Google Docs export via `scripts/export_to_google_docs.py`.

**Interfaces:**
- Consumes: real outputs/screens captured in Tasks 1–2; existing `bugfixes/`, `lab/`, `configs/`, `scripts/export_to_google_docs*.py`.

- [ ] **Step 1: Write `docs/AI_INFRA_RUNBOOK.md`** with these sections (audience: technical, new to GPU/Google AI infra):
  1. **Intro + glossary** — plain-language: GPU/CUDA/H100/NVLink-NVSwitch; GKE/node pool/pod/namespace; DWS Flex-Start; tensor parallelism; vLLM; JupyterHub.
  2. **Infrastructure** — cluster → node pools → A3/H100 → DWS Flex-Start (why, 7-day cap, holders, the reclaim-bug story from `bugfixes/0002`) → networking; reference the SP-0 `lab/`.
  3. **Inference service** — vLLM/Qwen3-32B, the deployment (paste the real manifests + `nvidia-smi`/curl output from Task 1), how to call it (curl + Python OpenAI client).
  4. **Jupyter host** — login, launch a GPU notebook, the `gpu_check`/`call_vllm` examples (paste real output).
  5. **Operations** — cost/holders, teardown (`rearm-holder.sh`), node rotation before expiry, capacity/reservation reality (`lab/RESERVATIONS.md`), troubleshooting (link `bugfixes/`).
  6. **Appendices** — exact commands, full manifests, architecture diagram (Mermaid).

- [ ] **Step 2: Verify the doc is complete & accurate**

```bash
grep -nE "TODO|TBD|<.*>" docs/AI_INFRA_RUNBOOK.md || echo "no placeholders"
# every GPU/GCP term used appears in the glossary:
for t in GPU CUDA H100 NVLink GKE "node pool" pod DWS vLLM JupyterHub "tensor parallel"; do grep -qi "$t" docs/AI_INFRA_RUNBOOK.md && echo "glossary/term OK: $t"; done
```
Expected: no placeholders; all terms present.

- [ ] **Step 3: Produce the Google Docs export**

```bash
python3 scripts/export_to_google_docs.py docs/AI_INFRA_RUNBOOK.md 2>&1 | tail -5 || \
  echo "[note] if the exporter expects a different arg, follow its --help; commit the generated HTML"
```
Expected: an HTML/Google-Docs artifact generated (commit it alongside).

- [ ] **Step 4: Commit**

```bash
git add docs/AI_INFRA_RUNBOOK.md
git commit -m "docs: full-stack AI infra runbook (cluster -> DWS -> inference -> Jupyter)"
```

---

## Self-Review

**Spec coverage:** §1 layout → Task 0/1/2 (nodeSelector+tolerations, 2+6 GPU split); §2 vLLM → Task 1 (TP=2, internal LB, PVC cache, /health); §3 Jupyter → Task 2 (Helm, GPU/CPU profiles, PD homes, examples, internal LB, dummy auth); §4 doc → Task 4 (6 sections, Markdown + Google Docs); §5 handoff → Task 1 Step 4 + Task 3 (scripts); §6 continuity → Task 1 (PD cache), Task 3 (PDB, 2-replica overlap, runbook); §7 testing → Task 1 Step 5, Task 2 Step 4, Task 4 Step 2. All covered.

**Placeholder scan:** Concrete manifests/commands throughout. Remaining intentional deploy-time values: `VLLM_IMAGE` tag (verify current stable in Task 0), the dummy-auth `password: REPLACE_AT_DEPLOY` (operator secret), and the optional `hf-token` Secret (only if Task 0 finds Qwen3-32B gated). These are documented inputs, not lazy placeholders. Helm/chart/image versions should be re-confirmed current at deploy.

**Type/name consistency:** `qwen3-vllm` (Deployment/Service/PDB), served model `qwen3-32b`, ns `inference`/`jupyter`, PVC `hf-cache`, storageClass `premium-rwo`, holder `a3-holder-zone-a`, node label `cloud.google.com/gke-accelerator=nvidia-h100-80gb`, and the in-cluster URL `qwen3-vllm.inference.svc.cluster.local:8000` are used consistently across Tasks 0–4.

**Known execution risks (documented):** kubectl/helm/auth-plugin install must succeed on the snap-managed box (Task 0 has a fallback); the A3 node expires ~July 23 (services are a demo); image/chart tags need currency check; the internal LB needs a proxy-only/appropriate subnet in the VPC.
