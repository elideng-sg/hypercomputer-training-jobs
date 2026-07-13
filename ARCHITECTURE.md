# Google Cloud AI Hypercomputer Cluster Architecture & Topology

This document provides complete system topology diagrams covering the execution pipelines, compute node hardware hierarchies, multi-zone resilient provisioning, and zero-binary REST interaction engines.

---

## 1. High-Level Deployment & Execution Topology

The cluster cleanly separates execution interaction across corporate workstations from remote control planes and scalable high-performance GPU compute tiers across Google Cloud infrastructure.

```mermaid
graph TD
    subgraph Client ["Client / Developer Workstation"]
        LocalTerminal["Terminal / Runbook Scripts"]
        EndpointGuard["Endpoint Security (Santa)"]
        DirectEngine["Option 1 Direct Engine<br>(03_submit_job_direct_gcloud.py)"]
        CloudShell["Google Cloud Shell Sandbox<br>(Linux Web & SSH Environment)"]

        LocalTerminal -->|If local binary allowed| LocalKubectl["kubectl CLI"]
        LocalTerminal -->|If blocked by security policy| DirectEngine
        DirectEngine -->|gcloud auth access token| REST["Secure HTTPS REST Request"]
    end

    subgraph GCP_Project ["GCP Project: hdlab-elideng (us-central1)"]
        subgraph QuotaLayer ["Compute & Quota Protection Layer"]
            RegionalQuota["Regional Compute Quota<br>(16x NVIDIA H100 80GB GPUs)"]
        end

        subgraph ControlPlane ["GKE Master Control Plane"]
            ClusterMaster["hypercomputer-a3-cluster<br>(Version: 1.36.0 | Channel: None)"]
            WorkloadId["Workload Identity Pool<br>(hdlab-elideng.svc.id.goog)"]
        end

        subgraph MultiZonePool ["Multi-Zone A3 High-GPU Node Pool (a3-h100-pool-8g)"]
            ZoneA["Zone: us-central1-a<br>(Managed Instance Group)"]
            ZoneB["Zone: us-central1-b<br>(Managed Instance Group)"]
            ZoneC["Zone: us-central1-c<br>(Managed Instance Group)"]
        end
    end

    REST ==>|HTTPS /apis/batch/v1/...| ClusterMaster
    CloudShell ==>|Unrestricted kubectl| ClusterMaster
    LocalKubectl -.->|Interrupted by Santa| EndpointGuard
    
    ClusterMaster -->|Auto-Schedules Workloads| MultiZonePool
    RegionalQuota -->|Enables Dynamic VM Allocation| MultiZonePool
    ZoneA <--> ZoneB <--> ZoneC
```

---

## 2. A3 Node Hardware & Pod Resource Interlock

Each physical `a3-highgpu-8g` compute instance integrates 8 concurrent NVIDIA H100 Tensor Core GPUs linked via high-speed interlocks alongside line-rate Google Virtual NIC (`gVNIC`) attachments.

```mermaid
graph TB
    subgraph A3Node ["GKE Kubernetes Node (a3-highgpu-8g | Container-Optimized OS 121)"]
        subgraph ComputeCPU ["Host Processor Architecture"]
            CPU["64x Host vCPUs"]
            SystemRAM["384 GiB Host Memory"]
        end

        subgraph StorageLayer ["Internal High-Capacity Storage"]
            NVMe["500 GB pd-ssd NVMe Local Scratch Space"]
            IPC_SHM["128 GiB In-Memory IPC Volume (/dev/shm)"]
        end

        subgraph NetworkLayer ["High-Speed Line-Rate Network Interfaces"]
            gVNIC["Google Virtual Network Interface Controller (gVNIC)"]
        end

        subgraph GPU_Topology ["Intra-Node 8x NVIDIA H100 80GB Interlock Topology"]
            GPU0["GPU 0: H100 80GB"]
            GPU1["GPU 1: H100 80GB"]
            GPU2["GPU 2: H100 80GB"]
            GPU3["GPU 3: H100 80GB"]
            GPU4["GPU 4: H100 80GB"]
            GPU5["GPU 5: H100 80GB"]
            GPU6["GPU 6: H100 80GB"]
            GPU7["GPU 7: H100 80GB"]

            NVLink["Intra-Node NVLink Switch Crossbar (Gen 5 / NVSwitch)"]
            GPU0 <--> NVLink
            GPU1 <--> NVLink
            GPU2 <--> NVLink
            GPU3 <--> NVLink
            GPU4 <--> NVLink
            GPU5 <--> NVLink
            GPU6 <--> NVLink
            GPU7 <--> NVLink
        end

        subgraph PyTorchPod ["Kubernetes Verification Workload Pod"]
            DDP["Container: verify-ddp-allreduce<br>(nvcr.io/nvidia/pytorch:24.03-py3)"]
            Torchrun["torchrun --nproc_per_node=8"]
            DDP --> Torchrun
        end
    end

    Torchrun ==>|Direct PCIe / NVLink Ring| GPU_Topology
    Torchrun ==>|Zero-Copy Tensor Buffers| IPC_SHM
    DDP <==>|Distributed Cross-Node Transfers| gVNIC
```

---

## 3. Option 1: Direct HTTPS REST Execution Flow

This sequence visualizes how [03_submit_job_direct_gcloud.py](file:///Users/elideng/hypercomputer-training-jobs/scripts/03_submit_job_direct_gcloud.py) deploys code, schedules jobs, and streams high-throughput diagnostics without invoking a single local binary blocked by corporate policies.

```mermaid
sequenceDiagram
    autonumber
    participant Client as Local Terminal Engine
    participant Auth as gcloud Auth Provider
    participant GKE as GKE REST Endpoint (/api/v1)
    participant KubeController as Kubernetes Job Controller
    participant GPU as A3 High-GPU Compute Node

    Client->>Auth: gcloud auth print-access-token
    Auth-->>Client: OAuth Bearer Token
    Client->>GKE: POST /api/v1/namespaces/default/configmaps<br>(Payload: train_benchmark_fp8.py)
    GKE-->>Client: ConfigMap created successfully
    Client->>GKE: POST /apis/batch/v1/namespaces/default/jobs<br>(Payload: Job Spec with nodeSelector a3-highgpu-8g)
    GKE-->>Client: Job scheduled onto cluster queue
    GKE->>KubeController: Trigger Pod instantiation loop
    KubeController->>GPU: Schedule Pod onto active H100 node across us-central1-a/b/c
    GPU->>GPU: Pull NVIDIA PyTorch container & mount /dev/shm
    loop Poll phase status (Every 10s)
        Client->>GKE: GET /api/v1/namespaces/default/pods/pod-name
        GKE-->>Client: Current Pod phase (Running / Succeeded)
    end
    Client->>GKE: GET /api/v1/namespaces/default/pods/pod-name/log?container=verify-ddp-allreduce
    GKE-->>Client: Stream live diagnostic logs & JSON performance report
```

---

## 4. Summary of Versioning & Quota Configuration Rules

| Configuration Factor | Implemented Setting | Rationale & Protection Target |
| :--- | :--- | :--- |
| **Control Plane Release Channel** | `--release-channel="None"` | Unenrolls cluster from forced auto-upgrade rules to permit custom node version pinning. |
| **Node Pool GKE Version** | `1.33.13-gke.1101000` | Specifically bundles **Container-Optimized OS (COS) 121**, explicitly required by `a3-highgpu-8g`. |
| **Node Pool Auto-Upgrades** | `--no-enable-autoupgrade` | Prevents background tasks from upgrading compute nodes to incompatible `COS 129 / GKE 1.36` kernels. |
| **Regional H100 Quota Limit** | `16` (in `us-central1`) | Allocates sufficient capacity across multiple available availability zones simultaneously. |
| **Node Location Deployment** | `us-central1-a/b/c` | Automatically reroutes node provisioning if any single zone suffers transient hardware stockouts. |
| **IPC POSIX Shared Memory** | `128Gi` (`/dev/shm`) | Eliminates out-of-memory zero-copy tensor crashes during distributed all-reduce operations. |
