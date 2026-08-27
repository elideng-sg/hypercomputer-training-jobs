# Deployment Part 4 — Deploy JupyterHub (GPU Notebooks)

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → **4. JupyterHub** → [5. Verify & Teardown](02e-verify-teardown.md)

---

> ### ⚠️ Historical: this describes the ORIGINAL `us-central1` build
>
> **The live deployment moved on 2026-08-05** to cluster `hypercomputer-a3-tcpxo` in
> **`asia-southeast1-c`** on **A3 Mega** nodes with the **GPUDirect-TCPXO** fabric. Read
> [Part 1's translation table](02a-cluster-setup.md) before copying any command here — the
> cluster name, region, machine type, and accelerator label have all changed, and
> `nvidia-h100-80gb` now matches **no node** (pods using it sit in `Pending` forever).
> Current manifests: [`deploy/tcpxo-migration/`](../../deploy/tcpxo-migration/).


**Part 4 of the deployment series.** Assumes the GPU node from [Part 2](02b-gpu-nodepool-dws.md) is running (the inference service from [Part 3](02c-deploy-inference.md) is recommended but not strictly required for this part).

This part installs **[JupyterHub](appendix-glossary.md#jupyterhub)** via Helm with a CPU profile and a GPU profile, so users can launch notebooks that land on the A3 node and request an H100. It uses `DummyAuthenticator` (demo password) on an internal LB as the base.

> For team access — public HTTPS + **Google sign-in** instead of the demo password — do this base install first, then follow **[Remote Access](05-remote-access-iap.md)**, which swaps in `GoogleOAuthenticator` and a public Ingress.

---

## Step 7: Deploy JupyterHub

### 7.1 Add the JupyterHub Helm repo

```bash
helm repo add jupyterhub https://hub.jupyter.org/helm-chart/
helm repo update
```

### 7.2 Create JupyterHub values file

Save this as `jupyter-values.yaml`:

```yaml
hub:
  config:
    JupyterHub:
      authenticator_class: dummy
    DummyAuthenticator:
      password: "demo2026"
proxy:
  service:
    type: LoadBalancer
    annotations:
      networking.gke.io/load-balancer-type: "Internal"
singleuser:
  storage:
    dynamic:
      storageClass: premium-rwo
    capacity: 20Gi
  profileList:
  - display_name: "CPU (no GPU)"
    default: true
    kubespawner_override:
      cpu_limit: 4
      mem_limit: "16G"
  - display_name: "GPU (1x H100)"
    kubespawner_override:
      image: quay.io/jupyter/pytorch-notebook:cuda12-latest
      extra_resource_limits:
        nvidia.com/gpu: "1"
      node_selector:
        cloud.google.com/gke-accelerator: nvidia-h100-80gb
      tolerations:
      - key: "nvidia.com/gpu"
        operator: "Exists"
        effect: "NoSchedule"
      - key: "cloud.google.com/gke-queued"
        operator: "Exists"
        effect: "NoSchedule"
```

**What this does:**
- **DummyAuthenticator** — any username, password `demo2026` (demo only; replace with Google OAuth for production)
- **Internal LoadBalancer** — private IP, VPC-only access
- **Two profiles:**
  - **CPU (default)** — 4 CPU / 16GB, no GPU
  - **GPU (1x H100)** — PyTorch CUDA 12 image, requests 1 GPU, schedules on H100 node
- **20Gi persistent home** per user

### 7.3 Install JupyterHub

```bash
helm upgrade --install jhub jupyterhub/jupyterhub \
  --namespace jupyter \
  --version 4.4.0 \
  --values jupyter-values.yaml \
  --timeout 10m
```

**Expected output:**
```
Release "jhub" does not exist. Installing it now.
NAME: jhub
LAST DEPLOYED: ...
NAMESPACE: jupyter
STATUS: deployed
```

**Time:** ~3-5 minutes.

**Verify pods:**
```bash
kubectl get pods -n jupyter
```

**Expected output:**
```
NAME                              READY   STATUS    RESTARTS   AGE
hub-xxxxxxxxxx-yyyyy              1/1     Running   0          2m
proxy-xxxxxxxxxx-zzzzz            1/1     Running   0          2m
user-scheduler-xxxxxxxxxx-aaaaa   1/1     Running   0          2m
```

### 7.4 Get the JupyterHub URL

```bash
kubectl get svc -n jupyter proxy-public
```

**Expected output:**
```
NAME           TYPE           CLUSTER-IP       EXTERNAL-IP       PORT(S)        AGE
proxy-public   LoadBalancer   10.108.yy.yy     10.128.15.234     80:xxxxx/TCP   3m
```

**JupyterHub URL:** `http://10.128.15.234` (internal IP, VPC-only).

### 7.5 Log in and launch a GPU notebook

**From a machine inside the VPC** (e.g., a GCP VM, or port-forward via a bastion):

1. Browse to `http://10.128.15.234`
2. Log in with any username and password **`demo2026`**
3. Select **"GPU (1x H100)"** from the profile dropdown
4. Click **Start My Server**
5. First launch takes 2-3 minutes (pulling the PyTorch CUDA image)

**Verify GPU access (in the notebook):**
```python
import torch
print(f"CUDA available: {torch.cuda.is_available()}")
print(f"Device: {torch.cuda.get_device_name(0)}")
```

**Expected output:**
```
CUDA available: True
Device: NVIDIA H100 80GB HBM3
```

### 7.6 Call vLLM from a notebook

**Example notebook cell:**
```python
from openai import OpenAI

client = OpenAI(
    base_url='http://qwen3-vllm.inference.svc.cluster.local:8000/v1',
    api_key='none'  # vLLM ignores this
)

response = client.chat.completions.create(
    model='qwen3-32b',
    messages=[{'role': 'user', 'content': 'Explain GPUs in one sentence.'}],
    max_tokens=128
)

print(response.choices[0].message.content)
```

**Expected output:**
```
A GPU (Graphics Processing Unit) is a specialized processor designed to accelerate
graphics rendering and parallel computations, making it ideal for AI and machine learning tasks.
```

**Success:** Notebooks can reach the inference service via in-cluster DNS.

### 7.7 Optional: per-user GCS workspaces

To give each user a private Cloud Storage bucket mounted at `~/gcs` — so datasets
and artifacts live in GCS instead of a 20 GB disk — deploy the add-on in
[`deploy/jupyter-gcs/`](../../deploy/jupyter-gcs/README.md).

It layers a Helm overlay on top of the values above and provisions a bucket, a
per-user Kubernetes ServiceAccount, and a Workload Identity binding on first
login. Three things to know before you start:

1. **The order is not optional.** `setup-iam.sh` must run before `install.sh`.
   The overlay re-exposes the cloud metadata server to user pods, which is only
   safe once each pod runs as its own ServiceAccount rather than the shared
   `default` one. `install.sh` preflights this and refuses to proceed otherwise.
2. **It needs the GCS FUSE CSI driver addon** on the cluster.
3. **It replaces the notebook images** with builds that carry `gcloud`, `gcsfs`
   and `google-cloud-storage`; the stock images have none of them.

```bash
deploy/jupyter-gcs/setup-iam.sh
deploy/jupyter-gcs/image/build.sh
deploy/jupyter-gcs/install.sh
```

The design rationale — including why this is a bucket per user rather than a
folder per user — is in
[the design spec](../superpowers/specs/2026-08-26-jupyter-gcs-workspaces-design.md).

---

← Previous: **[Part 3 — Deploy Inference](02c-deploy-inference.md)**  |  Next: **[Part 5 — Verify & Teardown](02e-verify-teardown.md)** →

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → **4. JupyterHub** → [5. Verify & Teardown](02e-verify-teardown.md)

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
