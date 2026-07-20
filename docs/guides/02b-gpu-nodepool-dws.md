# Deployment Part 2 — GPU Node Pool & DWS Provisioning

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → **2. GPU Node & DWS** → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

---

**Part 2 of the [deployment series](01-architecture.md#7-where-to-go-next).** You should have completed [Part 1 — Cluster Setup](02a-cluster-setup.md) first (a running regional GKE cluster with `kubectl` access).

This part creates the DWS-enabled A3 GPU node pool, provisions the 8× H100 node through Dynamic Workload Scheduler, and sets up the namespaces and model-cache storage the workloads need. This is the most involved part — DWS requires a **[ProvisioningRequest](appendix-glossary.md#provisioningrequest)** and a **[capacity holder](appendix-glossary.md#capacity-holder)** working together.

---

## Step 3: Create the GPU node pool (DWS-enabled)

### 3.1 Create the A3 H100 node pool

```bash
gcloud container node-pools create a3-h100-dws-pool \
  --cluster hypercomputer-a3-cluster \
  --region us-central1 \
  --machine-type a3-highgpu-8g \
  --accelerator type=nvidia-h100-80gb,count=8,gpu-driver-version=latest \
  --num-nodes 0 \
  --enable-autoscaling \
  --min-nodes 0 \
  --max-nodes 1 \
  --node-labels cloud.google.com/gke-accelerator=nvidia-h100-80gb,gpu-cluster=a3-h100 \
  --node-taints nvidia.com/gpu=present:NoSchedule \
  --scopes cloud-platform \
  --enable-queued-provisioning \
  --project $PROJECT_ID
```

**Expected output:**
```
Creating node pool a3-h100-dws-pool...done.
Created [https://container.googleapis.com/v1/projects/hdlab-elideng/zones/us-central1/nodePools/a3-h100-dws-pool].
NAME               MACHINE_TYPE      DISK_SIZE_GB  NODE_VERSION
a3-h100-dws-pool   a3-highgpu-8g     100           1.30.x-gke.y
```

**Time:** ~2 minutes to create the pool configuration (no node is provisioned yet).

**What this does:**
- Creates a node pool for **`a3-highgpu-8g`** machines (8× H100 80GB GPUs)
- **Starts at 0 nodes** (autoscale 0→1) for cost savings
- Enables **queued provisioning** (`--enable-queued-provisioning`) — this is **DWS Flex-Start**
- Applies a **taint** (`nvidia.com/gpu=present:NoSchedule`) so only GPU-aware pods schedule here
- Adds **labels** for pod nodeSelectors

**Key flags explained:**
- `--machine-type a3-highgpu-8g` — 8× H100 80GB, 208 vCPUs, 1872 GB RAM
- `--accelerator` — tells GKE to install GPU drivers (NVIDIA driver 535+, CUDA 12.0)
- `--num-nodes 0` — start with zero nodes (no GPU cost until we provision)
- `--enable-queued-provisioning` — **DWS Flex-Start** (provisions when capacity available, 7-day max runtime)
- `--node-taints` — keeps non-GPU pods off this expensive node

**Check the pool:**
```bash
gcloud container node-pools list \
  --cluster hypercomputer-a3-cluster \
  --region us-central1 \
  --project $PROJECT_ID
```

**Expected output:**
```
NAME               MACHINE_TYPE      DISK_SIZE_GB  NODE_VERSION
default-pool       e2-standard-4     100           1.30.x-gke.y
a3-h100-dws-pool   a3-highgpu-8g     100           1.30.x-gke.y
```

---

## Step 4: Provision the GPU node via DWS

This is the most complex step. DWS requires a **ProvisioningRequest** and a **consumer holder** to keep the node from being reclaimed.

![DWS Lifecycle](../diagrams/dws-lifecycle.svg)

**Figure 2: DWS lifecycle.** (1) Submit ProvisioningRequest + consumer holder. (2) DWS searches for capacity (may take minutes to hours). (3) Node provisions and holder binds within the 10-minute booking window. (4) Node stays up for 7 days (holder keeps it occupied). (5) Real workload can take over by scaling holder to 0. See `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md` for why the consumer pattern is required.

### 4.1 Submit the DWS ProvisioningRequest

Save this manifest as `dws-request.yaml`:

```yaml
apiVersion: autoscaling.x-k8s.io/v1
kind: ProvisioningRequest
metadata:
  name: a3-h100-req-zone-a
  namespace: default
spec:
  provisioningClassName: queued-provisioning.gke.io
  parameters:
    maxRunDurationSeconds: "604800"  # 7 days (max allowed)
  podSets:
  - count: 1
    podTemplateRef:
      name: a3-h100-pod-template-zone-a
---
apiVersion: v1
kind: PodTemplate
metadata:
  name: a3-h100-pod-template-zone-a
  namespace: default
template:
  metadata:
    labels:
      app: gpu-workload
      dws-zone: us-central1-a
  spec:
    restartPolicy: Never
    nodeSelector:
      node.kubernetes.io/instance-type: a3-highgpu-8g
      gpu-cluster: a3-h100
      topology.kubernetes.io/zone: us-central1-a  # Pin to zone a
    tolerations:
    - key: "nvidia.com/gpu"
      operator: "Exists"
      effect: "NoSchedule"
    - key: "cloud.google.com/gke-queued"
      operator: "Exists"
      effect: "NoSchedule"
    containers:
    - name: placeholder
      image: nvcr.io/nvidia/pytorch:24.03-py3
      resources:
        limits:
          nvidia.com/gpu: "8"
          cpu: "64"
          memory: "384Gi"
        requests:
          nvidia.com/gpu: "8"
          cpu: "64"
          memory: "384Gi"
```

**Apply it:**
```bash
kubectl apply -f dws-request.yaml
```

**What this does:**
- Submits a request for **1× a3-highgpu-8g** in zone `us-central1-a`
- Sets **`maxRunDurationSeconds: 604800`** (7 days, the maximum for Flex-Start)
- The pod template describes the shape DWS should provision (8 GPUs, full node resources)

**Check status:**
```bash
kubectl get provisioningrequests -A
```

**Expected output (immediately after submission):**
```
NAMESPACE   NAME                   AGE   ACCEPTED   PROVISIONED
default     a3-h100-req-zone-a     10s   True       False
```

**What `ACCEPTED=True` means:** GKE accepted the request and is searching for capacity. **This can take anywhere from 5 minutes to several hours** depending on regional H100 availability. You will see `PROVISIONED=True` when a node is assigned.

**Why zone-pinned:** The `topology.kubernetes.io/zone: us-central1-a` selector ensures this request targets zone `a` specifically. Without this, multiple per-zone requests are identical and GKE may place them all in one zone. See `bugfixes/0001-dws-zone-requests-not-zone-pinned.md` for the full story.

### 4.2 Deploy the capacity holder (consumer)

**Critical:** Without a **consumer holder**, the provisioned node will be **reclaimed ~10-15 minutes after boot**. DWS holds a fresh node for only ~10 minutes; a pod must land on it inside that window or the autoscaler removes it as "not needed." See `bugfixes/0002-dws-a3-node-reclaimed-after-10min.md` for the full diagnosis.

Save this manifest as `dws-holder.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: a3-holder-zone-a
  namespace: default
  labels:
    app: a3-holder
    dws-zone: us-central1-a
spec:
  replicas: 1
  selector:
    matchLabels:
      app: a3-holder-zone-a
  template:
    metadata:
      labels:
        app: a3-holder-zone-a
      annotations:
        # These two annotations are REQUIRED to consume the ProvisioningRequest
        autoscaling.x-k8s.io/consume-provisioning-request: a3-h100-req-zone-a
        autoscaling.x-k8s.io/provisioning-class-name: "queued-provisioning.gke.io"
        cluster-autoscaler.kubernetes.io/safe-to-evict: "false"
    spec:
      restartPolicy: Always
      nodeSelector:
        node.kubernetes.io/instance-type: a3-highgpu-8g
        gpu-cluster: a3-h100
        topology.kubernetes.io/zone: us-central1-a
      tolerations:
      - key: "nvidia.com/gpu"
        operator: "Exists"
        effect: "NoSchedule"
      - key: "cloud.google.com/gke-queued"
        operator: "Exists"
        effect: "NoSchedule"
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9  # Tiny placeholder container
        resources:
          limits:
            nvidia.com/gpu: "8"
            cpu: "64"
            memory: "384Gi"
          requests:
            nvidia.com/gpu: "8"
            cpu: "64"
            memory: "384Gi"
```

**Apply it:**
```bash
kubectl apply -f dws-holder.yaml
```

**What this does:**
- Deploys a **consumer holder** — a tiny `pause` container that requests the full 8-GPU shape
- Carries **both required annotations**:
  - `autoscaling.x-k8s.io/consume-provisioning-request: a3-h100-req-zone-a` — links to the request
  - `autoscaling.x-k8s.io/provisioning-class-name: "queued-provisioning.gke.io"` — declares DWS consumer
- Sets `safe-to-evict: false` — keeps the node from idle scale-down once holder binds
- **Starts immediately as `Pending`** and binds the instant the node provisions (inside the 10-min window)

**Check holder status:**
```bash
kubectl get pods -n default -l app=a3-holder-zone-a
```

**Expected output (while waiting for capacity):**
```
NAME                                  READY   STATUS    RESTARTS   AGE
a3-holder-zone-a-xxxxxxxxxx-yyyyy     0/1     Pending   0          30s
```

**Healthy signal (from events):**
```bash
kubectl describe pod -n default -l app=a3-holder-zone-a | grep IgnoredInScaleUp
```

**Expected output:**
```
IgnoredInScaleUp: Unschedulable pod ignored in scale-up loop, because it's consuming
ProvisioningRequest default/a3-h100-req-zone-a that is in Accepted state.
```

**What this means:** The autoscaler recognizes the holder as a request consumer and won't try to provision via other mechanisms. It's waiting for DWS to deliver the node.

**Broken signal (old, incorrect holder):**
```
no.scale.up.nap.pod.gpu.no.limit.defined
```
If you see this, the holder is missing the consume annotations and **will not hold the node** — the node will be reclaimed. Verify the YAML has both `autoscaling.x-k8s.io/*` annotations (not the old `cluster-autoscaler.kubernetes.io/` prefix).

### 4.3 Wait for provisioning (5 minutes to hours)

**Monitor the request:**
```bash
watch kubectl get provisioningrequests -A
```

**When capacity is found, you'll see:**
```
NAMESPACE   NAME                   AGE   ACCEPTED   PROVISIONED
default     a3-h100-req-zone-a     15m   True       True
```

**Check the node appeared:**
```bash
kubectl get nodes -L cloud.google.com/gke-accelerator
```

**Expected output:**
```
NAME                                                  STATUS   ROLES    AGE   ACCELERATOR
gke-hypercomputer-a3-default-pool-xxxx-yyyy           Ready    <none>   20m   <none>
gke-hypercomputer-a3-a3-h100-dws-pool-16664d9c-hhp6   Ready    <none>   2m    nvidia-h100-80gb
```

**Verify holder bound:**
```bash
kubectl get pods -n default -l app=a3-holder-zone-a -o wide
```

**Expected output:**
```
NAME                                  READY   STATUS    RESTARTS   AGE   NODE
a3-holder-zone-a-xxxxxxxxxx-yyyyy     1/1     Running   0          5m    gke-hypercomputer-a3-a3-h100-dws-pool-16664d9c-hhp6
```

**Success signal:** The holder is **`Running`** on the GPU node. The node will now stay up for **7 days** (until `maxRunDurationSeconds` expires or you delete the request).

**If the node vanishes ~15 minutes after boot:** You hit the reclaim bug. Check that the holder pod has **both** `autoscaling.x-k8s.io/consume-provisioning-request` and `autoscaling.x-k8s.io/provisioning-class-name` annotations in its pod spec (not just the Deployment). Re-apply the corrected holder and request.

---

## Step 5: Deploy namespaces and storage

### 5.1 Create namespaces

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: inference
---
apiVersion: v1
kind: Namespace
metadata:
  name: jupyter
EOF
```

**What this does:** Creates two namespaces to organize workloads:
- `inference` — for the vLLM inference service
- `jupyter` — for JupyterHub and notebook pods

### 5.2 Create the model cache PVC

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hf-cache
  namespace: inference
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: premium-rwo  # GKE pd-ssd dynamic class
  resources:
    requests:
      storage: 150Gi
EOF
```

**What this does:** Creates a **150Gi SSD-backed Persistent Volume** to cache the Qwen3-32B model weights (~60GB). Without this, every pod restart re-downloads the model from Hugging Face (slow and wasteful). The PVC is `ReadWriteOnce` (single-node attachment), which is fine since the inference pod runs on one node.

**Verify:**
```bash
kubectl get pvc -n inference
```

**Expected output:**
```
NAME       STATUS   VOLUME                                     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
hf-cache   Bound    pvc-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   150Gi      RWO            premium-rwo    10s
```

---

← Previous: **[Part 1 — Cluster Setup](02a-cluster-setup.md)**  |  Next: **[Part 3 — Deploy Inference](02c-deploy-inference.md)** →

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → **2. GPU Node & DWS** → [3. Inference](02c-deploy-inference.md) → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
