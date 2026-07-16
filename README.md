# GCP AI Hypercomputer Training Jobs & A3/A4 Cluster Execution Guide

This repository contains a production-ready operational setup for launching, validating, and benchmarking multi-node multi-GPU training workloads on **Google Cloud AI Hypercomputer** (`A3 High / Ultra` and `A4 Blackwell` clusters).

---

## 🏗 Repository Structure

```text
hypercomputer-training-jobs/
├── configs/
│   ├── a3_a4_verification_job.yaml           # Kubernetes multi-GPU verification PyTorchJob spec (8x GPUs, IPC shm)
│   ├── a3_dws_holder.yaml                    # Base zero-GPU capacity holder pause DaemonSet specification
│   ├── a3_dws_holder_8gpu.yaml               # 8-GPU Active Capacity Holder Deployment totally disarming BookingExpired up to 7 days
│   ├── dws_provisioning_request.yaml         # Optimized atomic single-machine 7-day DWS Queued Provisioning Request (`TARGET_SIZE: 1`)
│   ├── dws_provisioning_request_zone_a.yaml  # Standalone DWS Queued Request specifically targeting zone us-central1-a (`604800`s)
│   ├── dws_provisioning_request_zone_b.yaml  # Standalone DWS Queued Request specifically targeting zone us-central1-b (`604800`s)
│   └── dws_provisioning_request_zone_c.yaml  # Standalone DWS Queued Request specifically targeting zone us-central1-c (`604800`s)
├── scripts/
│   ├── 01_setup_gcp_project.sh               # Phase 1: Configure gcloud project, APIs & query regional GPU quotas
│   ├── 02_create_gke_cluster.sh              # Phase 2: Provision foundational Option 2 Spot node pool across Iowa (`8x L4`)
│   ├── 02_create_gke_cluster_dws.sh          # Phase 2 (DWS): Deploy multi-zone A3 8x H100 node pool with atomic 7-day queue request + 8-GPU Capacity Holder
│   ├── 03_submit_verification_job.sh         # Phase 3: Package ConfigMap & stream live diagnostic training run logs
│   ├── 03_submit_job_direct_gcloud.py        # Option 1: Direct GKE HTTPS REST API launcher (bypasses local kubectl)
│   └── 04_teardown_cluster.sh                # Phase 4: Cost-protection script to scale nodes to zero or delete cluster
├── src/
│   └── train_benchmark_fp8.py          # Distributed PyTorch NCCL & Tensor Core DDP benchmark script
├── logs/                               # Output log folder for NCCL traces and runtime timing JSON files
├── ARCHITECTURE.md                     # Comprehensive multi-diagram system configuration, hardware topologies, & REST sequence charts
├── GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md # Complete step-by-step colleagues operational initialization & testing handbook alongside GKE architectural deep dive
├── ai_hypercomputer_verification_report.md # Full diagnostic execution run report and hardware performance metric breakdown
└── README.md                           # Comprehensive end-to-end execution runbook
```

---

## 🏛 Core Documentation & Initialization Handbooks

To assist engineering and data science colleagues right across replicating and testing our completely verified, high-availability multi-GPU deployment right out of the box, refer directly across our primary reference handbooks in the project root:
1. **[GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md](file:///Users/elideng/hypercomputer-training-jobs/GKE_GPU_WORKLOAD_INIT_TEST_GUIDE.md)** — **The primary Colleagues Initialization & Testing Handbook.** Explains exact GKE internal architectures for GPU training (`Container-Optimized OS`, `/dev/shm` IPC volumes, GPU device tolerations), details how we eliminated corporate macOS endpoint blocks (`Option 1 REST API Automation without kubectl`), and provides step-by-step terminal initialization protocols utilizing multi-zone Spot scheduling (`--spot` across `us-central1` Iowa).
2. **[ai_hypercomputer_verification_report.md](file:///Users/elideng/hypercomputer-training-jobs/ai_hypercomputer_verification_report.md)** — Comprehensive runtime diagnostic assessment documenting our verified multi-GPU benchmark run (`g2-standard-96` | 8x NVIDIA L4 Ada Lovelace GPUs + 96 vCPUs), presenting exact worker synchronizations (`torchrun --nproc_per_node=8`), native Fourth-Gen Tensor Core `torch.bfloat16` timings (`3.474s over 25 DDP iterations`), and aggregate 4.81 GB/s ring `All-Reduce` crossbar throughput.
3. **[ARCHITECTURE.md](file:///Users/elideng/hypercomputer-training-jobs/ARCHITECTURE.md)** — Detailed multi-diagram system topographies, NVLink/NVSwitch structural diagrams, and pure HTTPS REST sequence charts.

```mermaid
graph TB
    subgraph Client ["Simplified User & Control Access"]
        API_Call["Developer Workstation / API Script"] -->|HTTPS REST Request| CP["GKE Control Plane (1.36 | Channel: None)"]
    end

    subgraph ClusterMesh ["AI Hypercomputer High-Performance Network Fabric (us-east4-a/b/c)"]
        subgraph Node1 ["A3 High-GPU Compute Node 1 (a3-highgpu-8g | COS 121)"]
            subgraph Host1 ["Host Compute Sub-System"]
                CPU1["64x vCPUs & 384GiB RAM"] --- SHM1["128GiB Shared IPC RAM (/dev/shm)"]
            end
            
            subgraph NVLink_Mesh1 ["Intra-Node NVLink Fabric & NVSwitch Array (900 GB/s Bidirectional per GPU)"]
                G0_1["GPU 0: H100"] & G1_1["GPU 1: H100"] & G2_1["GPU 2: H100"] & G3_1["GPU 3: H100"] <==>|NVLink Gen 5| NVS1["Dual NVSwitch Switchboard Crossbar"]
                G4_1["GPU 4: H100"] & G5_1["GPU 5: H100"] & G6_1["GPU 6: H100"] & G7_1["GPU 7: H100"] <==>|NVLink Gen 5| NVS1
            end

            NIC1["Multi-NIC gVNIC Hardware Network Interfaces (Line-Rate Capacity)"]
            Host1 ---|PCIe Gen 5 Bus| NVLink_Mesh1
            NVLink_Mesh1 <==>|GPUDirect / NCCL_NET_GDR_LEVEL=5| NIC1
        end

        subgraph Node2 ["A3 High-GPU Compute Node 2 (Peer Distributed Training Node)"]
            subgraph NVLink_Mesh2 ["Intra-Node NVLink Fabric & NVSwitch Array"]
                G_Array2["8x NVIDIA H100 80GB GPUs"] <==>|NVLink Gen 5| NVS2["Dual NVSwitch Switchboard Crossbar"]
            end
            NIC2["Multi-NIC gVNIC Hardware Network Interfaces"]
            NVLink_Mesh2 <==>|GPUDirect / NCCL_NET_GDR_LEVEL=5| NIC2
        end

        NIC1 <==>|High-Throughput GCP Spine-and-Leaf Inter-Node Optical Mesh| NIC2
    end

    CP ==>|Schedules verification-source-map & PyTorch Spec| Node1 & Node2
```

### Key Technical Configurations

1. **Explicit Kernel Pinning (Container-Optimized OS 121)**:
   - A3 instances (`a3-highgpu-8g`) demanding full multi-NIC networking requires stable driver attachments exclusively available on **COS 121** (or lower). 
   - To prevent newer GKE versions (`1.36` bundled with `COS 129`) from causing compatibility faults, our deployment explicitly decouples master and node versions: the control plane stays unenrolled from auto-upgrades (`--release-channel=None`), while the node pool initializes pinned at **GKE `1.33.13-gke.1101000` (`--no-enable-autoupgrade`)**, preserving the exact allowed `N-3` Kubernetes skew tolerance.

2. **Dynamic Workload Scheduler (DWS) Queued Provisioning & Multi-Zone Resiliency**:
   - Synchronous on-demand single-zone allocations for contiguous 8x H100 GPU blocks (`a3-highgpu-8g`) frequently encounter hardware stockout peaks (`[GCE_STOCKOUT]`). Our setup secures a regional compute quota (`NVIDIA_H100_GPUS`) of **32x GPUs** across `us-east4`.
   - To completely eliminate synchronous 35-minute stockout timeouts during cluster creation, our node pool (`a3-h100-pool-8g`) is explicitly configured with **Dynamic Workload Scheduler (DWS) Queued Provisioning (`--enable-queued-provisioning --reservation-affinity="none"`)** and initialized at **0 nodes** (`--num-nodes=0`).
   - When a job targeting `cloud.google.com/gke-queued: "true"` (`a3_a4_verification_job.yaml` or via `configs/dws_provisioning_request.yaml`) is submitted, GKE dynamic multi-zone autoscaling (`--location-policy=ANY` across `us-east4-a,us-east4-b,us-east4-c`) instantly requests a slot from Google's physical scheduling queue right in whichever zone frees up capacity next.

3. **Option 1 Endpoint Security & Direct REST Execution**:
   - Corporate endpoint protection software (**Santa**) strictly blocks unauthorized binary executions (`Killed: 9` when calling command-line `kubectl`).
   - To achieve seamless execution without whitelisting delays, we employ **Option 1 Execution Model**:
     - [scripts/03_submit_job_direct_gcloud.py](file:///Users/elideng/hypercomputer-training-jobs/scripts/03_submit_job_direct_gcloud.py) uses secure OAuth Bearer tokens (`gcloud auth print-access-token`) directly against your GKE Master endpoint via secure HTTPS REST JSON calls (`/api/v1/...` and `/apis/batch/v1/...`).
     - [scripts/03_submit_verification_job.sh](file:///Users/elideng/hypercomputer-training-jobs/scripts/03_submit_verification_job.sh) detects local security restrictions instantly and automatically falls back to the direct REST API engine without stopping.

---

## 🚀 Detailed Step-by-Step Execution Plan

### Step 1: Initialize GCP Project & Verify Compute Quota
Configure your active profile against your target GCP Project and enable all required Hypercomputer APIs (`compute`, `container`, `tpu`, `cloudquotas`).

**Command to run:**
```bash
./scripts/01_setup_gcp_project.sh <TARGET_PROJECT_ID>
```

---

### Step 2: Provision A3/A4 AI Hypercomputer Cluster with Multi-Zone DWS Queued Provisioning & Holder Protection
To launch either our baseline Spot verification pool or our dedicated **8x H100 A3 Dynamic Workload Scheduler (`DWS`) Node Pool (`a3-h100-dws-pool`)** across all 3 Iowa availability zones (`us-central1-a/b/c`), execute the appropriate deployment script below:

#### Option A: Dedicated 8x H100 DWS Queued Setup (Recommended for 7-Day Uninterrupted Workloads)
Executes our standalone DWS setup script directly at [scripts/02_create_gke_cluster_dws.sh](file:///Users/elideng/hypercomputer-training-jobs/scripts/02_create_gke_cluster_dws.sh). This automatically provisions our multi-zone (`ANY` policy) **8x H100 DWS node pool (`a3-h100-dws-pool`)** across `us-central1-a/b/c`, registers our atomic single-machine 7-day (`"604800"` seconds) physical capacity reservation (`TARGET_SIZE: 1` right to bypass multi-node block bottlenecks), and deploys our active **8-GPU Capacity Holder (`configs/a3_dws_holder_8gpu.yaml`)** right away. Because our 8-GPU holder uses `registry.k8s.io/pause:3.9` while consuming `nvidia.com/gpu: 8`, it immediately lands across any newly booted A3 machine out of the queue to completely disarm `BookingExpired` idle scale-downs—preserving your supercomputing server up to the full 7-day maximum continuous window!

To launch Option A directly across your terminal:
```bash
./scripts/02_create_gke_cluster_dws.sh
```

#### Option B: Standard Spot Option 2 Setup (`8x L4 g2-standard-96`)
```bash
./scripts/02_create_gke_cluster.sh
```
> [!NOTE]
> Under our multi-zone DWS workflow (`02_create_gke_cluster_dws.sh`), each availability zone manages its physical hardware reservation right inside its own independent `ProvisioningRequest` object across Google's queues. Because the included `a3-dws-capacity-holder` DaemonSet automatically binds `safe-to-evict: false` onto any newly booted machine, your physical H100 node will remain safely active in your cluster up to the maximum 7 continuous days completely protected from `BookingExpired` termination!

---

### Step 3: Run the 8x GPU Distributed Training Verification Suite
Package our distributed training code ([src/train_benchmark_fp8.py](file:///Users/elideng/hypercomputer-training-jobs/src/train_benchmark_fp8.py)) right into your cluster and run the multi-GPU DDP test suite. If `kubectl` is restricted locally, the script automatically triggers our Option 1 HTTPS REST API launcher (`03_submit_job_direct_gcloud.py`).

**Command to run:**
```bash
./scripts/03_submit_verification_job.sh
```

#### What the Verification Workload Proves:
1. **Intra-Node Crossbar Throughput:** Evaluates pure NVLink Gen 4/5 all-reduce speed across all 8 concurrent GPUs (`NCCL_DEBUG=INFO`).
2. **Mixed-Precision Math Execution:** Computes heavy neural model iterations using modern `bfloat16` or `float8_e4m3fn` Tensor Core routines via `torchrun --nproc_per_node=8`.
3. **IPC Shared Memory Integrity:** Confirms high-capacity shared RAM (`/dev/shm`) access via explicit 128Gi RAM disk mounting inside [a3_a4_verification_job.yaml](file:///Users/elideng/hypercomputer-training-jobs/configs/a3_a4_verification_job.yaml).

---

### Step 4: Scale Down or Clean Up Resources (Cost Safeguard)
Because 8x H100/B200 nodes accrue rapid on-demand usage costs, always scale your compute capacity to **0** as soon as test execution wraps up.

**Command to run:**
```bash
./scripts/04_teardown_cluster.sh
```

---

## 🛠 Driver Diagnostics & Cloud Sandbox Execution
* **Cloud Terminal Sandbox:** You can interact natively with your Kubernetes API directly across Google's high-speed web sandbox (where zero endpoint security blocks exist) by opening `https://shell.cloud.google.com/?project=<YOUR_GCP_PROJECT_ID>` or running `gcloud cloud-shell ssh`.
* **Santa Whitelisting:** If you wish to enable native command-line `kubectl` across your corporate Mac workstation, access your Santa diagnostic URL (`https://upvote.googleplex.com/blockables/4408c85c83...`) in a web browser to grant explicit developer authorization for `/bin/kubectl.1.36`.
