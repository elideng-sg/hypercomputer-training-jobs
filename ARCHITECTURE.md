# AI Hypercomputer Network & Hardware Topology Guide

This document defines the physical hardware architecture and network topologies of our **Google Cloud AI Hypercomputer (`a3-highgpu-8g`)** deployment across Google Kubernetes Engine (GKE). It specifically illustrates how multi-GPU distributed workloads achieve low-latency communication via **internal NVLink / NVSwitch crossbars** and **GPUDirect multi-NIC line-rate mesh networks**.

---

## 1. End-to-End Cluster Network Fabric Topology

Our architecture minimizes client access complexity into a simplified API entrypoint while heavily focusing high-throughput network engineering directly inside the node hardware tiers.

```mermaid
graph TB
    subgraph Access ["Simplified User & Control Access"]
        Client["Developer Workstation / API Script"] -->|HTTPS REST / OAuth Token| CP["GKE Master Control Plane (1.36 | Channel: None)"]
    end

    subgraph ClusterMesh ["AI Hypercomputer High-Performance Network Fabric (us-central1-a/b/c)"]
        subgraph Node1 ["A3 High-GPU Compute Node 1 (a3-highgpu-8g | COS 121)"]
            subgraph Host1 ["Host Compute Sub-System"]
                CPU1["64x vCPUs & 384GiB RAM"] --- SHM1["128GiB Shared IPC Mount (/dev/shm)"]
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

---

## 2. Intra-Node NVLink Crossbar & NCCL Ring Topology

Inside each physical 8-GPU node (`a3-highgpu-8g`), distributed all-reduce operations (`torchrun --nproc_per_node=8`) completely bypass host memory busses during tensor ring synchronizations using **NVIDIA Gen 5 NVLink Switch architectures**.

```mermaid
graph LR
    subgraph DualNVSwitch ["Internal NVSwitch High-Speed Backbone"]
        Switch1["NVSwitch Chip 0"] 
        Switch2["NVSwitch Chip 1"]
    end

    subgraph GPU_Bank_A ["PCIe Socket / Bank A"]
        GPU0["GPU 0 (H100 80GB)"]
        GPU1["GPU 1 (H100 80GB)"]
        GPU2["GPU 2 (H100 80GB)"]
        GPU3["GPU 3 (H100 80GB)"]
    end

    subgraph GPU_Bank_B ["PCIe Socket / Bank B"]
        GPU4["GPU 4 (H100 80GB)"]
        GPU5["GPU 5 (H100 80GB)"]
        GPU6["GPU 6 (H100 80GB)"]
        GPU7["GPU 7 (H100 80GB)"]
    end

    GPU0 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU1 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU2 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU3 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2

    GPU4 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU5 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU6 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
    GPU7 <==>|18x NVLink Lanes - 900 GB/s| Switch1 & Switch2
```

### Technical Highlights:
- **Bandwidth Capacity:** Each individual NVIDIA H100 80GB GPU pushes **900 GB/s** of total bidirectional NVLink bandwidth directly across the internal NVSwitch backplane.
- **Ring Synchronization:** During gradient synchronization steps (`NCCL_ALGORITHM=RING`), data packets flow simultaneously in full concurrent rings (`GPU 0 -> GPU 1 -> GPU 2 -> ... -> GPU 7 -> GPU 0`) across dedicated hardware lanes without inter-socket latency penalties.

---

## 3. GPUDirect Memory Bypass & Line-Rate gVNIC Interlocks

When scaling from a single 8-GPU node to multi-node training clusters (`A3 High / Ultra` or `A4 Blackwell`), network saturation between distinct physical machines is mitigated by enabling **Google Virtual Network Interface Controller (`gVNIC`)** hardware acceleration combined with **NVIDIA GPUDirect (`NCCL_NET_GDR_LEVEL=5`)**.

```mermaid
graph TD
    subgraph Node_A ["Local Compute Node A"]
        H100_A["NVIDIA H100 GPU Memory (HBM3)"]
        PCIe_A["PCIe Gen 5 Host Bus Switch"]
        gVNIC_A["gVNIC Multi-NIC Hardware Adapter"]
        CPU_Buffer_A["Host CPU OS Memory Buffers<br>(Bypassed during GPUDirect Transfers)"]
        
        H100_A <==>|Direct Zero-Copy DMA Pathway| PCIe_A
        PCIe_A <==>|Direct Zero-Copy DMA Pathway| gVNIC_A
        H100_A -.-x|Bypass Mode Enabled via GDR=5| CPU_Buffer_A
    end

    subgraph Node_B ["Remote Compute Node B"]
        gVNIC_B["gVNIC Multi-NIC Hardware Adapter"]
        PCIe_B["PCIe Gen 5 Host Bus Switch"]
        H100_B["NVIDIA H100 GPU Memory (HBM3)"]
        
        gVNIC_B <==>|Direct Zero-Copy DMA Pathway| PCIe_B
        PCIe_B <==>|Direct Zero-Copy DMA Pathway| H100_B
    end

    gVNIC_A <==>|Google Hypercomputer Optical Inter-Node Fabric - Line-Rate Speed| gVNIC_B
```

### Why GPUDirect (`NCCL_NET_GDR_LEVEL=5`) & gVNIC are Critical:
1. **Standard Traditional Network Flow (Legacy Bottleneck):**
   `GPU Memory (HBM3) -> PCIe -> Host CPU System RAM (/dev/shm) -> CPU Network Stack Processing -> Physical Network Adapter -> Wire` *(Requires CPU interrupts and double memory staging pauses)*.
2. **Our GPUDirect Zero-Copy Accelerated Pathway:**
   `GPU Memory (HBM3) <=== Direct DMA PCIe Bus ===> gVNIC Hardware Interface <=== Optical Mesh ===> Peer gVNIC <=== Direct DMA ===> Remote GPU HBM3` *(Zero CPU interruption, minimum micro-second network serialization jitter)*.

---

## 4. Simplified User API & Option 1 Execution Sequence

With network hardware topologies handling computation internally across the cluster, our user interaction model reduces down to straightforward REST endpoints authenticated directly via Google Cloud IAM OAuth.

```mermaid
sequenceDiagram
    autonumber
    participant Dev as Local API Launcher / CLI Script
    participant API as GKE Master API (REST endpoint /api/v1)
    participant Pool as Multi-Zone Node Pool (a3-h100-pool-8g)
    participant Fabric as NVLink & gVNIC Hardware Topology

    Dev->>API: POST ConfigMap verification-source-map (train_benchmark_fp8.py)
    Dev->>API: POST batch/v1 Job (gcp-ai-hypercomputer-verification)
    API->>Pool: Instantiate Pod on available H100 node across us-central1-a/b/c
    Pool->>Fabric: Mount /dev/shm (128Gi) & attach 8x H100 NVSwitches
    Fabric->>Fabric: Execute torchrun distributed training & ring all-reduce benchmarks
    Dev->>API: Stream diagnostics over HTTP HTTPS GET (/pods/name/log)
    API-->>Dev: Return live NCCL timing traces & JSON precision reports to local logs/
```

---

## 5. Summary Matrix of Cluster Network Engineering Tokens

| Parameter Flag / Setting | Applied Target | Architectural Rationale |
| :--- | :--- | :--- |
| **`--accelerator=type=nvidia-h100-80gb,count=8`** | `gcloud container node-pools create` | Attaches exactly 8 full H100 80GB GPUs backed by internal NVLink Gen 5 and dual NVSwitch architectures. |
| **`--enable-gvnic`** | `gcloud container node-pools create` | Activates hardware line-rate Google Virtual NICs (`gVNIC`), delivering maximum packet rates required for multi-node inter-node scaling. |
| **`NCCL_NET_GDR_LEVEL=5`** | `a3_a4_verification_job.yaml` Container Env | Instructs NVIDIA NCCL to use GPUDirect RDMA across direct PCIe-to-network pathways, totally bypassing host CPU RAM copies during cluster exchanges. |
| **`NCCL_ALGORITHM=RING`** | `a3_a4_verification_job.yaml` Container Env | Enforces deterministic ring gradient summation protocols perfectly mirroring the circular physical NVSwitch interlock topology. |
| **`128Gi /dev/shm` (In-Memory `emptyDir`)** | `a3_a4_verification_job.yaml` Spec Volumes | Mounts a 128 GiB POSIX tmpfs ramdisk across all 8 training sub-processes (`torchrun --nproc_per_node=8`), ensuring local IPC data tensor transfers never crash out-of-memory. |
| **`--node-version=1.33.13-gke.1101000`** | `gcloud container node-pools create` | Guarantees **Container-Optimized OS (COS) 121**, which maintains validated NVIDIA LTSB device drivers fully certified for stable `gVNIC` multi-NIC throughput without packet drops. |
