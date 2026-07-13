# GCP AI Hypercomputer Training Jobs & A3/A4 Cluster Execution Guide

This repository contains a production-ready operational setup for launching, validating, and benchmarking multi-node multi-GPU training workloads on **Google Cloud AI Hypercomputer** (`A3 High / Ultra` and `A4 Blackwell` clusters).

---

## 🏗 Repository Structure

```text
hypercomputer-training-jobs/
├── configs/
│   └── a3_a4_verification_job.yaml     # Kubernetes multi-GPU verification PyTorchJob spec (8x GPUs, IPC shm)
├── scripts/
│   ├── 01_setup_gcp_project.sh          # Phase 1: Configure gcloud project, APIs & query regional GPU quotas
│   ├── 02_create_gke_cluster.sh         # Phase 2: Provision GKE cluster with multi-NIC gVNIC A3/A4 node pool
│   ├── 03_submit_verification_job.sh    # Phase 3: Package ConfigMap & stream live diagnostic training run logs
│   └── 04_teardown_cluster.sh           # Phase 4: Cost-protection script to scale nodes to zero or delete cluster
├── src/
│   └── train_benchmark_fp8.py          # Distributed PyTorch NCCL & Tensor Core DDP benchmark script
├── logs/                               # Output log folder for NCCL traces and runtime timing JSON files
└── README.md                           # Comprehensive end-to-end execution runbook
```

---

## 🚀 Detailed Step-by-Step Execution Plan

### Step 1: Initialize GCP Project & Verify Compute Quota
Configure your active authentication profile against your target GCP Project and enable all required Hypercomputer APIs (`compute`, `container`, `tpu`, `storage`).

**Command to run:**
```bash
./scripts/01_setup_gcp_project.sh <TARGET_PROJECT_ID>
```

---

### Step 2: Provision the A3/A4 AI Hypercomputer Cluster
Launch an enterprise GKE Cluster and provision the **8x GPU A3 Node Pool (`a3-highgpu-8g`)** featuring automatic NVIDIA LTSB drivers and hardware `gVNIC` multi-network attachments.

**Command to run:**
```bash
./scripts/02_create_gke_cluster.sh
```
> [!NOTE]
> To deploy higher-tier Blackwell or H200 instances instead, edit `MACHINE_TYPE` inside `02_create_gke_cluster.sh` to `a3-ultragpu-8g` (H200) or `a4-highgpu-8g` (Blackwell B200) before running.

---

### Step 3: Run the 8x GPU Distributed Training Verification Suite
Once your node pool reports `Ready`, package our multi-GPU DDP training code ([src/train_benchmark_fp8.py](file:///Users/elideng/hypercomputer-training-jobs/src/train_benchmark_fp8.py)) into a Kubernetes ConfigMap and trigger execution. The script will automatically tail diagnostics and export precision metric timings into [logs/](file:///Users/elideng/hypercomputer-training-jobs/logs).

**Command to run:**
```bash
./scripts/03_submit_verification_job.sh
```

#### What the Verification Workload Proves:
1. **Intra-Node Crossbar Throughput:** Evaluates pure NVLink Gen 4/5 all-reduce speed across all 8 concurrent GPUs (`NCCL_DEBUG=INFO`).
2. **Mixed-Precision Math Execution:** Computes heavy neural model iterations using modern `bfloat16` or `float8_e4m3fn` Tensor Core routines via `torchrun --nproc_per_node=8`.
3. **IPC Shared Memory Integrity:** Confirms high-capacity shared RAM (`/dev/shm`) access via explicit 128Gi RAM disk mounting in our containerspec.

---

### Step 4: Scale Down or Clean Up Resources (Cost Safeguard)
Because 8x H100/B200 nodes accrue rapid on-demand usage costs, always scale your compute capacity to **0** as soon as test execution verification wraps up.

**Command to run:**
```bash
./scripts/04_teardown_cluster.sh
```

---

## 🛠 Driver Diagnostics & Gotchas
If encountering unexpected connection or driver issues during initial setup:
* **GPU Persistence:** Our automated deployment ensures `nvidia-persistenced` stays running on each node kernel to prevent high driver attach latency.
* **XID Diagnostics:** Run `kubectl describe nodes` and check kernel log entries for XID fault codes if an instance abruptly exits during training loops.
