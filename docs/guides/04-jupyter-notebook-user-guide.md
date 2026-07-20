# Jupyter Notebook User Guide — GPU-Powered Notebooks

**Audience:** Engineers, data scientists, and technical staff who want to use Jupyter notebooks with GPU support for AI/ML experimentation and development.

**What this guide covers:** How to log into JupyterHub, choose a GPU or CPU profile, verify GPU access, call the inference endpoint, install packages, manage persistence, and troubleshoot common issues.

**Prerequisites:** Basic familiarity with Jupyter notebooks and Python. No GPU or Kubernetes knowledge required.

> **New to the terminology?** Terms like JupyterHub, H100, and tensor parallelism are defined in the **[Glossary appendix](appendix-glossary.md)**.

---

## Table of Contents

1. [What JupyterHub Is](#1-what-jupyterhub-is)
2. [Log In](#2-log-in)
3. [Choose a Profile](#3-choose-a-profile)
4. [Launch and Verify GPU Access](#4-launch-and-verify-gpu-access)
5. [Call the Inference Endpoint from Your Notebook](#5-call-the-inference-endpoint-from-your-notebook)
6. [Install Packages and Do AI/ML Work](#6-install-packages-and-do-aiml-work)
7. [Persistence — What Survives Restarts](#7-persistence--what-survives-restarts)
8. [Shut Down Your Server](#8-shut-down-your-server)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What JupyterHub Is

**JupyterHub** is a multi-user Jupyter notebook environment. Instead of running Jupyter locally on your laptop, you log into a shared hub that spawns a private notebook server for you in the cloud.

![Jupyter flow](../diagrams/jupyter-flow.svg)

**Figure 1: Jupyter notebook spawn and model call flow.** (1) User logs into JupyterHub at the internal load balancer. (2) User selects a profile (GPU or CPU) and clicks Start My Server. (3) Hub spawns a notebook pod on the appropriate node (GPU notebooks land on the A3 node with H100 GPUs). (4) User opens a notebook, writes Python code, and calls the vLLM inference endpoint using the in-cluster DNS name. (5) Request routes to the vLLM pod, which generates a response.

### Key features

- **Per-user isolation:** Each user gets their own notebook server pod with dedicated resources
- **GPU or CPU profiles:** Choose between a GPU-powered notebook (1× H100 GPU) or a CPU-only notebook
- **Persistent home directory:** Your files are saved to a 20 GB persistent volume that survives server restarts
- **Public access with Google sign-in:** Reachable at **`https://jupyter.34.54.187.199.nip.io`** — sign in with your Workspace Google account (restricted to your organization's domain). No VPN or `kubectl` needed.

### Live deployment values

- **JupyterHub version:** 5.5.0
- **Public URL:** `https://jupyter.34.54.187.199.nip.io` (HTTPS, Google sign-in)
- **GPU profile:** 1× H100 GPU, PyTorch + CUDA 12, `quay.io/jupyter/pytorch-notebook:cuda12-latest` image
- **CPU profile:** 4 CPU cores, 16 GB RAM, no GPU
- **Storage per user:** 20 GB persistent disk (GCP `premium-rwo` SSD)

---

## 2. Log In

### Step 1: Open JupyterHub

In a web browser, go to:

```
https://jupyter.34.54.187.199.nip.io
```

Works from anywhere — no VPN, no `kubectl`.

### Step 2: Sign in with Google

Click **Sign in with Google** and use your **Workspace account** (`@your-domain`). Only accounts in your organization's domain are allowed — there is no shared password. Your first sign-in provisions your personal 20 GB home directory.

**Access control:** Who can log in is governed by the domain restriction (and optionally an explicit allow-list) configured by your admin — see the [Remote Access guide](05-remote-access-iap.md). If you're denied, ask your admin to add you.

---

## 3. Choose a Profile

After logging in, you'll see a profile selection page with a dropdown offering the two profiles below. Pick one, then click **Start My Server**.

### Available profiles

| Profile | Resources | When to Use | Image |
|---------|-----------|-------------|-------|
| **CPU (no GPU)** | 4 CPU cores, 16 GB RAM | Data analysis, non-GPU workloads, small models, general Python development | Standard JupyterLab image |
| **GPU (1x H100)** | 1× NVIDIA H100 80GB GPU, PyTorch, CUDA 12 | Training/fine-tuning models, running GPU-accelerated libraries (PyTorch, TensorFlow, JAX), large-scale inference | `quay.io/jupyter/pytorch-notebook:cuda12-latest` |

### Which profile should I choose?

- **Choose CPU** if you're doing data exploration, visualization, or running code that doesn't need a GPU
- **Choose GPU** if you're:
  - Training or fine-tuning machine learning models
  - Running GPU-accelerated libraries (PyTorch, TensorFlow, JAX, cuDF, etc.)
  - Experimenting with large models or datasets that benefit from GPU parallelism

**Important: GPUs are shared and scarce.** The cluster has a single A3 node with 8 H100 GPUs. The vLLM inference service uses 2 GPUs, leaving approximately **6 GPUs available** for notebooks. If all 6 are in use, your GPU notebook will remain in "Pending" state until a GPU becomes available. Please shut down your GPU server when you're done (see [Section 8](#8-shut-down-your-server)).

---

## 4. Launch and Verify GPU Access

### Step 1: Start your server

1. Select your profile (e.g., **"GPU (1x H100)"**)
2. Click **Start My Server**
3. Wait for the server to start:
   - **First launch:** May take 2-3 minutes while the PyTorch CUDA image is pulled (approximately 5 GB)
   - **Subsequent launches:** Typically 15-30 seconds (image is cached)

You'll see a progress bar and status messages like "Waiting for server to start..." and "Server is starting...".

Once the server is ready, JupyterLab will open in your browser.

### Step 2: Verify GPU access

**Example notebook:** See `deploy/jupyter/examples/gpu_check.ipynb` for a ready-to-run example.

#### Option 1: Run `nvidia-smi` in a terminal

1. In JupyterLab, open a **Terminal** (File > New > Terminal)
2. Run:

```bash
nvidia-smi
```

**Sample output:**

```
Fri Jul 17 10:48:03 2026
+---------------------------------------------------------------------------------------+
| NVIDIA-SMI 535.309.01             Driver Version: 535.309.01   CUDA Version: 12.2     |
|-----------------------------------------+----------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |         Memory-Usage | GPU-Util  Compute M. |
|                                         |                      |               MIG M. |
|=========================================+======================+======================|
|   0  NVIDIA H100 80GB HBM3          Off | 00000000:04:00.0 Off |                    0 |
| N/A   33C    P0              71W / 700W |      0MiB / 81559MiB |      0%      Default |
+---------------------------------------------------------------------------------------+
```

If you see "NVIDIA H100 80GB HBM3", the GPU is available.

#### Option 2: Run Python code in a notebook

Create a new notebook (File > New > Notebook) and run:

```python
!nvidia-smi
```

Then, in another cell:

```python
import torch

print(f"CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"Device name: {torch.cuda.get_device_name(0)}")
    print(f"Device count: {torch.cuda.device_count()}")
```

**Sample output:**

```
CUDA available: True
Device name: NVIDIA H100 80GB HBM3
Device count: 1
```

If `torch.cuda.is_available()` returns `True`, PyTorch can see the GPU and you're ready to run GPU-accelerated code.

---

## 5. Call the Inference Endpoint from Your Notebook

Your notebook pod runs inside the Kubernetes cluster, so it can reach the vLLM inference endpoint via **in-cluster DNS** without needing an external IP or port-forward.

**Example notebook:** See `deploy/jupyter/examples/call_vllm.ipynb` for a ready-to-run example.

### Step 1: Install the OpenAI client (if needed)

In a notebook cell:

```python
!pip install -q openai
```

(The `-q` flag suppresses verbose output. The PyTorch notebook image may already have `openai` installed, but this ensures it's available.)

### Step 2: Call the inference endpoint

```python
from openai import OpenAI

# In-cluster DNS name for the vLLM service. The endpoint now requires the API key.
import os
client = OpenAI(
    base_url="http://qwen3-vllm.inference.svc.cluster.local:8000/v1",
    api_key=os.environ.get("VLLM_API_KEY", "<paste the key from your admin>"),
)

response = client.chat.completions.create(
    model="qwen3-32b",
    messages=[{"role": "user", "content": "What is 2+2?"}],
    max_tokens=20
)

print(response.choices[0].message.content)
```

**Sample output:**

```
<think>
Okay, so I need to figure out what 2 plus 2 is. Let me
```

(Truncated at `max_tokens=20`. Increase `max_tokens` for longer responses.)

### Why use the DNS name?

The DNS name `qwen3-vllm.inference.svc.cluster.local:8000` resolves to the vLLM Service within the cluster. This works even though your notebook is in the `jupyter` namespace and vLLM is in the `inference` namespace — Kubernetes DNS resolves `<service>.<namespace>.svc.cluster.local` cluster-wide.

**Alternative:** From outside the cluster, use the public endpoint `https://infer.136.69.110.10.nip.io/v1` with the same API key. Inside a notebook, the in-cluster DNS name above is simplest (and avoids leaving the cluster network).

### More examples

For detailed examples of streaming, multi-turn conversations, and parameter tuning, see the **[Inference Endpoint User Guide](03-inference-endpoint-user-guide.md)**.

---

## 6. Install Packages and Do AI/ML Work

### Install Python packages with pip

You can install any Python package in your notebook:

```python
!pip install pandas scikit-learn matplotlib seaborn transformers accelerate
```

**Note:** Packages installed this way are **not persistent** across server restarts. If you frequently use a set of packages, consider:

- Adding them to a `requirements.txt` file in your home directory and running `!pip install -r requirements.txt` in a startup cell
- (Advanced) Building a custom notebook image with pre-installed packages

### Where files live

- **Your home directory (`/home/jovyan`):** Persistent, survives server restarts (backed by a 20 GB persistent disk)
- **Installed packages:** Stored in the container filesystem (not persistent; you'll need to reinstall after a server restart)
- **Running kernel state (variables, imports):** Only in memory; lost when the kernel restarts or the server stops

**Best practice:** Save your work frequently. Use notebooks (`.ipynb` files) for interactive work and Python scripts (`.py` files) for reusable code. Both are saved to your home directory and will persist.

### Example: GPU-accelerated PyTorch training

```python
import torch
import torch.nn as nn
import torch.optim as optim

# Verify GPU is available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

# Define a simple model
model = nn.Sequential(
    nn.Linear(784, 256),
    nn.ReLU(),
    nn.Linear(256, 10)
).to(device)

# Dummy data (replace with your actual dataset)
x = torch.randn(64, 784).to(device)
y = torch.randint(0, 10, (64,)).to(device)

# Training loop
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=0.001)

for epoch in range(10):
    optimizer.zero_grad()
    outputs = model(x)
    loss = criterion(outputs, y)
    loss.backward()
    optimizer.step()
    print(f"Epoch {epoch+1}, Loss: {loss.item():.4f}")
```

This code will run on the H100 GPU, providing significantly faster training than a CPU.

---

## 7. Persistence — What Survives Restarts

### Persistent (survives restarts)

- **Your home directory (`/home/jovyan`):** All notebooks, scripts, data files, and subdirectories you create are saved to a 20 GB persistent disk (GCP `premium-rwo` SSD)
- **File contents:** Everything written to disk in your home directory

### Not persistent (lost on restart)

- **Installed packages:** Python packages installed with `pip` are stored in the container's ephemeral filesystem and are lost when the server stops. You'll need to reinstall them.
- **Kernel state:** Variables, imported modules, and running processes are only in memory. If you stop the server or the pod is restarted, kernel state is lost.
- **Temporary files outside home:** Files written to `/tmp` or other paths outside `/home/jovyan` are not persistent.

### What triggers a restart?

- **Manual stop:** You stop the server from the JupyterHub control panel (File > Hub Control Panel > Stop My Server)
- **Idle culling:** If your server is idle for too long (no notebook activity), the hub may automatically cull (stop) it to free up resources (default: 60 minutes of inactivity)
- **Node maintenance or pod restart:** If the underlying node is drained or the pod crashes, Kubernetes will recreate your server pod, and your home directory will reattach

### Best practices

- Save notebooks frequently (`Ctrl+S` or `Cmd+S`)
- Keep a `requirements.txt` file in your home directory with your commonly used packages:

```bash
# In a terminal or notebook cell
cat > requirements.txt <<EOF
torch
transformers
accelerate
pandas
matplotlib
seaborn
EOF
```

Then, in a startup cell of your notebook:

```python
!pip install -q -r requirements.txt
```

---

## 8. Shut Down Your Server

**Important: GPUs are shared and scarce.** When you're done working, please stop your GPU server to free up the GPU for other users.

### How to stop your server

1. In JupyterLab, go to **File > Hub Control Panel** (or navigate to `https://jupyter.34.54.187.199.nip.io/hub/home`)
2. Click the **Stop My Server** button
3. Wait for the server to stop (the button will change to "Start My Server")

Your files in `/home/jovyan` are safe and will be available when you start your server again.

### Idle culling

To prevent resource waste, the JupyterHub is configured to **automatically cull idle servers** after a period of inactivity (default: 60 minutes). If your notebook has no activity (no cells executing, no terminals open with active processes), the hub will stop your server.

You'll receive a notification when you return, and you can restart your server with one click. Your saved notebooks and files will still be there.

### How to avoid idle culling

If you're running a long training job or background process and don't want your server to be culled:

- Keep a notebook open with a cell that periodically prints or logs (e.g., a training loop with progress bars)
- Run your long job in a notebook cell (not a terminal), as notebook activity resets the idle timer

**Note:** Even with idle culling disabled, please stop your server when you're truly done to free up the GPU for others.

---

## 9. Troubleshooting

### Problem: Server won't start (stuck in "Pending" state)

**Symptoms:** After clicking "Start My Server" and selecting the GPU profile, the server stays in "Pending" or "Waiting for server..." state for more than 5 minutes.

**Likely causes:**

| Cause | How to check | Fix |
|-------|--------------|-----|
| All GPUs are in use | Ask other users if they're using GPU notebooks | Wait for a GPU to free up, or ask others to stop their servers when done |
| GPU node is down | (Admin) `kubectl get nodes -l cloud.google.com/gke-accelerator=nvidia-h100-80gb` | (Admin) The A3 GPU node may need to be reprovisioned (see [Architecture Reference](01-architecture.md) for DWS details) |
| Image pull is slow (first launch) | Wait a bit longer (2-3 minutes) | The PyTorch CUDA image is approximately 5 GB and takes time to pull on first launch |

**Workaround:** Select the **"CPU (no GPU)"** profile instead if you don't strictly need GPU acceleration for your current task.

### Problem: Lost work or notebook is empty

**Symptoms:** You return to JupyterHub and your notebook is gone, or cells you wrote are missing.

**Likely causes:**

- You didn't save the notebook before stopping the server
- The notebook file was saved outside your home directory (e.g., in `/tmp`)

**Fix:**

- Always save your notebooks frequently (`Ctrl+S` or `Cmd+S`)
- Verify notebooks are saved in `/home/jovyan` or a subdirectory (this is the default location in JupyterLab)
- Check for autosave: JupyterLab creates `.ipynb_checkpoints` directories with recent autosaved versions

**Note:** Kernel state (variables, outputs) is not persistent. Only the notebook file (code cells and markdown) persists. If you need to preserve outputs, consider exporting the notebook as HTML or PDF (File > Save and Export Notebook As...).

### Problem: Package import errors or "ModuleNotFoundError"

**Symptoms:**

```python
ModuleNotFoundError: No module named 'transformers'
```

**Causes:**

- The package isn't installed in the current server session
- You installed it in a previous session, but the server was restarted (packages are not persistent)

**Fix:**

Install the package again:

```python
!pip install transformers
```

For packages you use frequently, keep a `requirements.txt` file and install from it at the start of each session (see [Section 6](#6-install-packages-and-do-aiml-work)).

### Problem: GPU not available (`torch.cuda.is_available()` returns `False`)

**Symptoms:**

```python
torch.cuda.is_available()
# False
```

or

```bash
nvidia-smi
# Command 'nvidia-smi' not found
```

**Causes:**

- You selected the **CPU (no GPU)** profile, not the **GPU (1x H100)** profile
- The GPU notebook pod failed to schedule on the GPU node (rare)

**Fix:**

1. Stop your server (File > Hub Control Panel > Stop My Server)
2. Start a new server and select **"GPU (1x H100)"** from the profile dropdown
3. Verify GPU access with `nvidia-smi` or `torch.cuda.is_available()` (see [Section 4](#4-launch-and-verify-gpu-access))

### Problem: Notebook kernel dies or "Kernel Restarting" message

**Symptoms:** The notebook kernel crashes with "Kernel Restarting" or "Kernel died, restarting" message.

**Likely causes:**

- Out of memory (GPU or CPU memory)
- Code that crashes Python (e.g., segfault in a native library)

**Fix:**

- **Reduce memory usage:** If you're loading a large model or dataset, try reducing batch size, using smaller models, or clearing variables (`del large_variable; import gc; gc.collect()`)
- **Check GPU memory:** In a terminal, run `nvidia-smi` to see GPU memory usage. If close to 81,559 MiB (full), you're out of GPU memory.
- **Restart kernel and rerun:** Kernel > Restart Kernel and Clear All Outputs, then rerun your notebook cells one by one to identify the problematic cell

### Problem: Cannot reach the inference endpoint

**Symptoms:**

```python
requests.exceptions.ConnectionError: ('Connection aborted.', ConnectionRefusedError(111, 'Connection refused'))
```

**Causes:**

- Wrong URL (using the external IP instead of the in-cluster DNS)
- vLLM service is down

**Fix:**

1. **Use the in-cluster DNS name** and include the API key (the endpoint now requires it): `http://qwen3-vllm.inference.svc.cluster.local:8000/v1`
2. **Check if vLLM is running:** In a terminal, run:

```bash
curl http://qwen3-vllm.inference.svc.cluster.local:8000/v1/models \
  -H "Authorization: Bearer $VLLM_API_KEY"
```

If you get a connection error, the vLLM service may be down. (Admin: check `kubectl -n inference get pods -l app=qwen3-vllm`)

See the **[Inference Endpoint User Guide](03-inference-endpoint-user-guide.md)** for more troubleshooting tips.

---

## Related Guides

- **[Inference Endpoint User Guide](03-inference-endpoint-user-guide.md)** — Detailed guide to calling the vLLM API (curl, Python, streaming, parameters)
- **[Architecture Reference](01-architecture.md)** — Understand the full system: GKE cluster, GPU nodes, vLLM deployment, JupyterHub, and networking
- **[Deployment from Scratch (Part 1 — start here)](02a-cluster-setup.md)** — The five-part series to deploy this infrastructure from a fresh GCP project
- **[Glossary appendix](appendix-glossary.md)** — Plain-language definitions of every term used in these guides

---

**Document version:** 2026-07-20  
**JupyterHub details:** Version 5.5.0 at `https://jupyter.34.54.187.199.nip.io` (public HTTPS, Google sign-in), GPU profile with 1× H100 80GB, CPU profile with 4 cores / 16 GB RAM, 20 GB persistent storage per user
