# Deployment Part 4 — Deploy JupyterHub (GPU Notebooks)

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → **4. JupyterHub** → [5. Verify & Teardown](02e-verify-teardown.md)

---

**Part 4 of the deployment series.** Assumes the GPU node from [Part 2](02b-gpu-nodepool-dws.md) is running (the inference service from [Part 3](02c-deploy-inference.md) is recommended but not strictly required for this part).

This part installs **[JupyterHub](appendix-glossary.md#jupyterhub)** via Helm with a CPU profile and a GPU profile, so users can launch notebooks that land on the A3 node and request an H100.

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

---

← Previous: **[Part 3 — Deploy Inference](02c-deploy-inference.md)**  |  Next: **[Part 5 — Verify & Teardown](02e-verify-teardown.md)** →

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → **4. JupyterHub** → [5. Verify & Teardown](02e-verify-teardown.md)

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
