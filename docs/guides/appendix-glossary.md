# Appendix — Glossary of Essential Concepts

**Audience:** Anyone reading the AI-infrastructure guides who is new to GPUs, Google Cloud, or Kubernetes.

This appendix defines every technical term used across the guides, in plain language. You do **not** need to read it top to bottom — the guides link here on first use of each term, so you can jump to a definition and jump back. Terms are grouped into four categories:

- [GPU and hardware terms](#gpu-and-hardware-terms)
- [Google Cloud and Kubernetes terms](#google-cloud-and-kubernetes-terms)
- [DWS and GPU provisioning terms](#dws-and-gpu-provisioning-terms)
- [Software and model terms](#software-and-model-terms)

---

## GPU and hardware terms

#### GPU

Graphics Processing Unit — a specialized processor with thousands of small cores optimized for the massively parallel math (matrix multiplications) that neural networks need. Modern large language models (LLMs) run on GPUs, not CPUs, because a CPU would be orders of magnitude too slow for the computations required.

#### H100

NVIDIA's Hopper-generation data-center GPU. Our variant is the **H100 80GB HBM3**, which has approximately 81,559 MiB of high-bandwidth memory. Big models like [Qwen3-32B](#qwen3-32b) need this much memory to load the model weights and process requests.

#### HBM

High-Bandwidth Memory — the fast memory physically stacked on the GPU chip. When we say "80GB H100," the 80GB refers to HBM capacity. Model weights and the data being processed (activations) live in this memory.

#### NVLink and NVSwitch

NVIDIA's high-speed GPU-to-GPU interconnect technology. Inside a single 8-GPU machine, **NVLink** connects GPUs directly and **NVSwitch** is the crossbar switch that lets all 8 GPUs communicate with each other at full bandwidth. This makes splitting a model across multiple GPUs on the *same* machine very fast (see [tensor parallelism](#tensor-parallelism)).

#### A3 machine

Google Cloud's "A3 High" machine type (`a3-highgpu-8g`): one virtual machine (VM) with **8× H100 80GB** GPUs all wired together with [NVLink and NVSwitch](#nvlink-and-nvswitch). This is the single GPU machine at the center of this architecture.

#### CUDA

NVIDIA's software platform and driver stack that lets programs use GPUs. Software is compiled against a **CUDA version**; the GPU **driver** installed on the machine must support that CUDA version or newer. Version mismatches can cause crashes — this is a real issue in this deployment (see the CUDA-version constraint in the [Inference deployment guide](02c-deploy-inference.md)).

#### DCGM

Data Center GPU Manager — NVIDIA's tool for monitoring GPU health, temperature, clock speeds, memory errors, and telemetry. Used in validation tests to confirm GPUs are healthy before handing them to workloads.

#### NCCL

NVIDIA Collective Communications Library — a library for multi-GPU and multi-node communication. An "all-reduce" test (exchanging data between all GPUs) verifies that GPUs can communicate over [NVLink](#nvlink-and-nvswitch) without errors.

#### Tensor parallelism

A technique for running a model that's too large or too slow for a single GPU by **splitting each layer's weight matrices across multiple GPUs**. The GPUs then compute in lockstep, exchanging partial results over [NVLink](#nvlink-and-nvswitch). Our system uses `--tensor-parallel-size 2`, so [Qwen3-32B](#qwen3-32b) is split across 2 of the 8 H100s.

---

## Google Cloud and Kubernetes terms

#### GCP

Google Cloud Platform — Google's cloud computing platform that provides virtual machines, storage, networking, and managed services.

#### VPC

Virtual Private Cloud — the private network your cloud resources live in. Resources in a VPC can communicate privately using internal IP addresses (like `10.128.x.x` in our deployment). Internal load balancer IPs are only reachable from inside the VPC, not from the public internet.

#### Region and zone

A **region** is a geographic area (like `us-central1` for Iowa, USA) containing multiple **zones** (independent data centers like `us-central1-a`, `us-central1-b`, etc.). Resources in the same zone can communicate with the lowest latency.

#### GKE

Google Kubernetes Engine — Google's managed [Kubernetes](#kubernetes) service. Google runs the control plane (the master nodes that schedule everything); you declare what you want (pods, services, deployments) and GKE schedules it onto worker nodes.

#### Kubernetes

An open-source platform (often abbreviated **K8s**) for automating deployment, scaling, and management of containerized applications. Think of it as an operating system for the cluster: you tell it what to run, and it figures out where and how.

#### Node

One machine (virtual machine) that belongs to the cluster and runs workloads ([pods](#pod)). Our GPU node is a single [A3 machine](#a3-machine) with 8 H100 GPUs.

#### Node pool

A group of identical [nodes](#node) that GKE manages as a unit. A node pool can autoscale (add or remove nodes based on demand). Ours is `a3-h100-dws-pool`, which contains one A3 GPU node.

#### Pod

The smallest deployable unit in Kubernetes: one or more [containers](#container) that are scheduled together on a [node](#node) and share networking and storage. The vLLM inference server runs as a pod; each user's Jupyter notebook also runs as a separate pod.

#### Container

A lightweight, standalone package of software that includes the application code, runtime, libraries, and dependencies. Containers are isolated from each other and from the host system. Docker is the most common container technology; Kubernetes orchestrates containers via [pods](#pod).

#### Namespace

A logical partition of the cluster for organizing and isolating workloads. We use three namespaces:

- `inference` — for the vLLM inference service
- `jupyter` — for JupyterHub and notebook pods
- `default` — for the [capacity holder](#capacity-holder)

#### Deployment

A Kubernetes object that keeps a specified number of identical [pod](#pod) replicas running and handles rollouts and rollbacks. If a pod crashes, the Deployment automatically creates a new one to replace it.

#### Service

A stable network endpoint in front of one or more [pods](#pod). Even if pods are recreated (new IP addresses), the Service IP stays the same. A [LoadBalancer](#loadbalancer-internal) Service gets an IP address that clients can connect to.

#### LoadBalancer (internal)

A type of Kubernetes [Service](#service) that gets a load balancer IP address. With the `Internal` annotation on GCP, it gets a **private** IP address reachable only inside the [VPC](#vpc) (not from the public internet). Both our services (vLLM and JupyterHub) use internal load balancers so they're only accessible from within the private network.

#### PVC and Persistent Disk

A **PersistentVolumeClaim (PVC)** is a request for durable storage that outlives a [pod](#pod). When a pod is deleted and recreated, data on a PVC persists. A PVC is backed by a **GCP Persistent Disk** (a network-attached SSD or HDD). We use a PVC to cache the downloaded [Qwen3-32B](#qwen3-32b) model weights (60+ GB) so pod restarts don't re-download the model.

#### Helm

Kubernetes' package manager. A **Helm chart** is a pre-packaged application (like JupyterHub) and a **`values.yaml`** file is the configuration you provide to customize it. Helm lets you deploy complex applications with a single command.

---

## DWS and GPU provisioning terms

#### DWS Flex-Start

**Dynamic Workload Scheduler (DWS) — Flex-Start mode** — a Google Cloud mechanism for obtaining scarce GPU capacity. You submit a request, and GKE provisions the GPU [node](#node) **when capacity becomes available** (you wait in a queue). Once provisioned, the node is held for up to a **hard 7-day maximum run duration**. Flex-Start is cheaper and easier to obtain than on-demand or reserved GPUs, but the trade-offs are:

- You **wait** for capacity (hours to days)
- The node has a **7-day cap** (no trick extends it)
- It's still expensive while running (you're just getting allocated capacity, not discounted capacity)

#### ProvisioningRequest

The Kubernetes object that [DWS](#dws-flex-start) uses to request capacity. A [pod](#pod) must actively **consume** the request (via specific annotations) to claim the node once it arrives, or GKE will reclaim the node after about 10 minutes (see the reclaim-bug story in the [DWS section of the Architecture Reference](01-architecture.md#3-how-gpus-are-obtained--dws-flex-start)).

#### Capacity holder

A tiny placeholder pod (using the `pause` container, which does nothing but sleep) that occupies the GPU [node](#node) to prevent GKE from scaling it away when no real workload is running. **House rule:** never leave the DWS GPU node idle and unheld — always re-arm the holder immediately after tearing down a workload, to avoid losing the scarce GPU node.

#### Taint and toleration

A **taint** marks a [node](#node) so that ordinary [pods](#pod) avoid it; a pod with a matching **toleration** is allowed to schedule there. GPU and DWS nodes are tainted so only GPU-aware pods (those that explicitly request GPUs) land on them. This prevents non-GPU pods from wasting the expensive GPU node.

#### nodeSelector

A rule in a [pod](#pod) specification to pick [nodes](#node) by label. For example, `cloud.google.com/gke-accelerator: nvidia-h100-80gb` targets the H100 node. Only nodes with that label will be considered for scheduling this pod.

---

## Software and model terms

#### vLLM

A high-throughput open-source LLM inference server. It loads a model from Hugging Face and exposes an [OpenAI-compatible](#openai-compatible-api) HTTP API (`/v1/models`, `/v1/chat/completions`). This means existing code written for OpenAI's API can work with a self-hosted model by just changing the base URL.

#### Qwen3-32B

The approximately 32-billion-parameter open-weights language model we're serving. It's available on Hugging Face without access restrictions (ungated), so no API token is needed to download it.

#### OpenAI-compatible API

An HTTP API that mimics OpenAI's endpoints and JSON schema. This lets the official `openai` Python client (or simple `curl` commands) talk to a self-hosted model as if it were OpenAI's service.

#### JupyterHub

A multi-user Jupyter notebook environment. A **hub** authenticates users and spawns a private notebook server [pod](#pod) for each user. Our JupyterHub offers two profiles: a CPU-only profile (no GPU) and a GPU profile that requests 1 H100 GPU and lands on the [A3 node](#a3-machine).

#### Jupyter notebook

An interactive computing environment (think: web-based Python REPL with saved state) where you can write and execute code, visualize data, and document your work all in one document. Popular for data science and machine learning experimentation.

#### PDB

PodDisruptionBudget — a Kubernetes rule that limits how many replicas of a workload can be voluntarily evicted (removed) at once. This protects availability during node maintenance or upgrades. Our inference service has a PDB requiring at least 1 pod to be running, so GKE won't drain the node if it would take down the only vLLM pod.

---

**Back to the guides:** [Architecture Reference](01-architecture.md) · [Deployment (start here)](02a-cluster-setup.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md)
