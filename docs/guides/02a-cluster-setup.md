# Deployment Part 1 — Cluster Setup (Project, Quota, GKE)

**Deployment series:** **1. Cluster Setup** → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

---

**Audience:** Technical engineers who are **new to GPUs and Google Cloud** but comfortable with the command line and basic Kubernetes concepts. This is **Part 1** of a five-part series that walks through deploying the complete stack from zero: GKE cluster, H100 GPUs via DWS, vLLM inference (Qwen3-32B), and JupyterHub with GPU notebooks.

> **New to the terminology?** Terms like GKE, DWS, node pool, and tensor parallelism are defined in the **[Glossary appendix](appendix-glossary.md)**.

**What the full series deploys:**

- A regional GKE cluster in Google Cloud
- An 8× H100 GPU node (A3 machine) provisioned through Dynamic Workload Scheduler (DWS)
- vLLM serving Qwen3-32B via an OpenAI-compatible API (internal endpoint)
- JupyterHub providing GPU-enabled Jupyter notebooks
- All infrastructure managed declaratively through manifests and scripts

**Time to complete (full series):** ~2-3 hours, including waiting for DWS capacity (minutes to hours depending on regional GPU availability).

**This part (Part 1)** covers prerequisites, project setup, GPU quota, and creating the regional GKE cluster.

---

## Prerequisites & Concepts

### What you need

**1. Google Cloud Project with billing enabled**
- A GCP project you have `Editor` or `Owner` permissions on
- Billing account linked to the project (GPUs incur significant cost)
- Budget alerts recommended (H100 nodes are ~$30/hour)

**2. Local tools installed**
```bash
# Google Cloud SDK (gcloud)
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud --version

# kubectl (Kubernetes CLI)
gcloud components install kubectl
kubectl version --client

# Helm (Kubernetes package manager, for JupyterHub)
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version
```

**3. IAM permissions**
You need permission to:
- Enable APIs
- Create GKE clusters, node pools, and networking resources
- Create Persistent Disks and Services (LoadBalancers)

Minimum recommended role: **`roles/editor`** on the project.

### What we're building (3-sentence overview)

You will deploy a **regional GKE cluster** with a **GPU node pool** that provisions an **8× H100 machine** on demand through DWS (Dynamic Workload Scheduler — Google's mechanism for obtaining scarce GPU capacity). The GPU node will run a **vLLM inference server** (using 2 of 8 H100s) serving the Qwen3-32B language model, and a **JupyterHub** instance that lets users launch GPU notebooks (consuming the remaining GPUs). All services are private (internal load balancers, no public internet exposure).

**Key architectural insight:** DWS Flex-Start gives you access to scarce H100 GPUs without reserving them 24/7, but enforces a hard **7-day maximum runtime**. A **capacity holder** keeps the node from being reclaimed during that window when no real workload is running.

![GKE Node Pools](../diagrams/gke-node-pools.svg)

**Figure 1: Node pool architecture.** The cluster has two pools: a system pool (CPU nodes, always-on) and the GPU pool (DWS-provisioned, 0-1 nodes). The GPU node is tainted so only GPU-aware workloads schedule there.

---

## Step 0: Set up your project

### 0.1 Configure gcloud CLI

```bash
# Set your project ID (replace with your project)
export PROJECT_ID=hdlab-elideng
gcloud config set project $PROJECT_ID
gcloud config set compute/region us-central1

# Authenticate if needed
gcloud auth login
gcloud auth application-default login
```

**What this does:** Configures the `gcloud` command-line tool to use your project and default region. All subsequent commands will target this project.

### 0.2 Enable required APIs

```bash
gcloud services enable \
  compute.googleapis.com \
  container.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  --project $PROJECT_ID
```

**Expected output:**
```
Operation "operations/..." finished successfully.
```

**What this does:** Activates the Google Cloud APIs needed to create VMs (Compute Engine), GKE clusters, and IAM resources. This is a prerequisite for any infrastructure work.

**Why these APIs:**
- `compute.googleapis.com` — for VMs, disks, networking
- `container.googleapis.com` — for GKE
- `iam.googleapis.com` — for service account permissions
- `cloudresourcemanager.googleapis.com` — for project-level operations

---

## Step 1: Request GPU quota

**Critical:** Default quotas are **insufficient** for H100 GPUs. You must request quota increases before creating the node pool.

### 1.1 Check current quota

```bash
gcloud compute regions describe us-central1 \
  --project $PROJECT_ID \
  --format="table(quotas.metric,quotas.limit)" \
  | grep -E "NVIDIA_H100|CPUS"
```

**Expected output (before requesting):**
```
quotas.metric                  quotas.limit
NVIDIA_H100_GPUS               0.0
CPUS                           24.0
```

### 1.2 Request H100 quota increase

**For DWS Flex-Start (our path):**
- Metric: `NVIDIA_H100_GPUS`
- Region: `us-central1`
- New limit: **8** (one A3 node = 8× H100)

**How to request:**
1. Go to [Quotas page](https://console.cloud.google.com/iam-admin/quotas)
2. Filter: `NVIDIA_H100_GPUS`, region `us-central1`
3. Select the quota, click **Edit Quotas**
4. Request **8 GPUs**
5. Provide business justification: "AI model inference testing"

**Processing time:** Typically 1-2 business days for H100s (scarce capacity; approval not guaranteed). For faster experimentation, consider **L4** or **A100** GPUs (more widely available).

**Why DWS Flex-Start:** H100 capacity is extremely scarce. DWS Flex-Start lets you request capacity and GKE provisions it when available, with a **7-day maximum runtime**. This is easier to get approved than on-demand reservations. For longer-lived capacity (>7 days), see `lab/RESERVATIONS.md` in the repo.

---

## Step 2: Create the GKE cluster

### 2.1 Create a regional cluster

```bash
gcloud container clusters create hypercomputer-a3-cluster \
  --region us-central1 \
  --release-channel regular \
  --machine-type e2-standard-4 \
  --num-nodes 1 \
  --enable-autoscaling \
  --min-nodes 1 \
  --max-nodes 3 \
  --enable-stackdriver-kubernetes \
  --scopes cloud-platform \
  --project $PROJECT_ID
```

**Expected output:**
```
Creating cluster hypercomputer-a3-cluster...done.
Created [https://container.googleapis.com/v1/projects/hdlab-elideng/zones/us-central1/clusters/hypercomputer-a3-cluster].
kubeconfig entry generated for hypercomputer-a3-cluster.
NAME                        LOCATION       MASTER_VERSION  NUM_NODES  STATUS
hypercomputer-a3-cluster    us-central1    1.30.x-gke.y    3          RUNNING
```

**Time:** ~5-10 minutes.

**What this does:**
- Creates a **regional** (multi-zone) cluster in `us-central1`
- Creates the **default node pool** (CPU nodes, `e2-standard-4`, 1 node per zone = 3 total)
- Enables autoscaling (1-3 nodes) for cost efficiency
- Uses `regular` release channel (stable updates)
- Grants `cloud-platform` scope (full API access for nodes)

**Why regional:** Higher availability (control plane and nodes span 3 zones). If one zone fails, workloads can move to others. GPU node pools can be zone-specific within the regional cluster.

### 2.2 Get cluster credentials

```bash
gcloud container clusters get-credentials hypercomputer-a3-cluster \
  --region us-central1 \
  --project $PROJECT_ID
```

**Expected output:**
```
Fetching cluster endpoint and auth data.
kubeconfig entry generated for hypercomputer-a3-cluster.
```

**Verify access:**
```bash
kubectl get nodes
```

**Expected output:**
```
NAME                                                  STATUS   ROLES    AGE   VERSION
gke-hypercomputer-a3-default-pool-xxxx-yyyy           Ready    <none>   5m    v1.30.x-gke.y
gke-hypercomputer-a3-default-pool-xxxx-zzzz           Ready    <none>   5m    v1.30.x-gke.y
gke-hypercomputer-a3-default-pool-xxxx-aaaa           Ready    <none>   5m    v1.30.x-gke.y
```

**What this does:** Downloads cluster credentials and configures `kubectl` to point at your new cluster. All subsequent `kubectl` commands will target this cluster.

---

Next: **[Part 2 — GPU Node Pool & DWS](02b-gpu-nodepool-dws.md)** →

**Deployment series:** **1. Cluster Setup** → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
