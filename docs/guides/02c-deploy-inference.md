# Deployment Part 3 — Deploy the Inference Service (vLLM + Qwen3-32B)

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → **3. Inference** → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

---

**Part 3 of the deployment series.** Requires a provisioned GPU node from [Part 2 — GPU Node Pool & DWS](02b-gpu-nodepool-dws.md).

This part deploys the **[vLLM](appendix-glossary.md#vllm)** inference server serving **[Qwen3-32B](appendix-glossary.md#qwen3-32b)** across 2 of the 8 H100 GPUs, hands the node off from the holder to vLLM gap-free, and exposes it on an internal load balancer.

---

## Step 6: Deploy the vLLM inference service

### 6.1 Deploy vLLM (Qwen3-32B, tensor parallel = 2)

```bash
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qwen3-vllm
  namespace: inference
  labels:
    app: qwen3-vllm
spec:
  replicas: 1
  selector:
    matchLabels:
      app: qwen3-vllm
  template:
    metadata:
      labels:
        app: qwen3-vllm
    spec:
      nodeSelector:
        cloud.google.com/gke-accelerator: nvidia-h100-80gb
      tolerations:
      - key: "nvidia.com/gpu"
        operator: "Exists"
        effect: "NoSchedule"
      - key: "cloud.google.com/gke-queued"
        operator: "Exists"
        effect: "NoSchedule"
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.8.4  # CUDA 12.0 compat + Qwen3 support
        args:
        - "--model"
        - "Qwen/Qwen3-32B"
        - "--served-model-name"
        - "qwen3-32b"
        - "--tensor-parallel-size"
        - "2"
        - "--gpu-memory-utilization"
        - "0.90"
        - "--max-model-len"
        - "32768"
        - "--host"
        - "0.0.0.0"
        - "--port"
        - "8000"
        env:
        - name: HF_HOME
          value: /hf-cache
        ports:
        - containerPort: 8000
        resources:
          limits:
            nvidia.com/gpu: "2"
            cpu: "24"
            memory: "200Gi"
          requests:
            nvidia.com/gpu: "2"
            cpu: "24"
            memory: "200Gi"
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 60
          periodSeconds: 15
          failureThreshold: 40
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 600
          periodSeconds: 30
        volumeMounts:
        - name: hf-cache
          mountPath: /hf-cache
        - name: shm
          mountPath: /dev/shm
      volumes:
      - name: hf-cache
        persistentVolumeClaim:
          claimName: hf-cache
      - name: shm
        emptyDir:
          medium: Memory
          sizeLimit: 16Gi
EOF
```

**What this does:**
- Deploys **vLLM v0.8.4** serving **Qwen3-32B** (ungated model, no Hugging Face token needed)
- **`--tensor-parallel-size 2`** — splits the model across **2 of 8 H100s** (~77GB per GPU)
- **`--gpu-memory-utilization 0.90`** — uses 90% of GPU memory for model/KV cache
- **`--max-model-len 32768`** — supports up to 32K token context
- Mounts **`hf-cache` PVC** at `/hf-cache` — model downloads once and persists
- Mounts **16Gi `/dev/shm`** — required for tensor parallelism (inter-GPU shared memory IPC)
- **nodeSelector + tolerations** — schedules onto the H100 node

**Why v0.8.4 (not latest):** The A3 node ships NVIDIA driver 535, which supports **CUDA 12.0**. Newer vLLM images (v0.25+) are built for CUDA 12.2+ and **crash-loop** on this driver with "Engine core initialization failed." v0.8.4 is CUDA 12.0-compatible and already supports Qwen3. Always verify the image's CUDA version against the node driver before upgrading.

**Why 16Gi `/dev/shm`:** Tensor parallelism uses shared memory for fast inter-GPU communication. The container default is 64MB, which causes OOM/IPC errors under TP. 16Gi is safe for TP=2.

### 6.2 Hand off the node from holder to vLLM (gap-free)

When vLLM starts scheduling, we can release the holder so vLLM takes over:

```bash
# Wait for vLLM pod to be scheduled (may take 2-5 min to download model on first run)
kubectl wait --for=jsonpath='{.status.phase}'=Running \
  pod -l app=qwen3-vllm -n inference --timeout=10m

# Release the holder (vLLM now holds the node)
kubectl -n default scale deploy/a3-holder-zone-a --replicas=0
```

**What this does:** Scales the holder to 0 replicas after vLLM binds. vLLM's 2-GPU request is enough to keep the node occupied (autoscaler won't remove it). This is a **gap-free handoff** — the node is never idle between holder and workload.

**Check vLLM is running:**
```bash
kubectl get pods -n inference -o wide
```

**Expected output:**
```
NAME                           READY   STATUS    RESTARTS   AGE   NODE
qwen3-vllm-xxxxxxxxxx-yyyyy    1/1     Running   0          5m    gke-hypercomputer-a3-a3-h100-dws-pool-16664d9c-hhp6
```

**Check logs (model loading):**
```bash
kubectl logs -n inference -l app=qwen3-vllm --tail=30 -f
```

**You should see:**
```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 6.3 Expose vLLM via internal LoadBalancer

```bash
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: qwen3-vllm
  namespace: inference
  annotations:
    networking.gke.io/load-balancer-type: "Internal"
spec:
  type: LoadBalancer
  selector:
    app: qwen3-vllm
  ports:
  - port: 8000
    targetPort: 8000
    protocol: TCP
EOF
```

**What this does:** Creates an **internal LoadBalancer** (private IP, VPC-only) in front of the vLLM pod. The `Internal` annotation ensures it's **not** exposed to the public internet.

**Get the internal IP:**
```bash
kubectl get svc -n inference qwen3-vllm
```

**Expected output:**
```
NAME         TYPE           CLUSTER-IP       EXTERNAL-IP     PORT(S)          AGE
qwen3-vllm   LoadBalancer   10.108.xx.xx     10.128.0.43     8000:xxxxx/TCP   30s
```

The **EXTERNAL-IP** (despite the name) is the **internal** load balancer IP (e.g., `10.128.0.43`). This IP is reachable from within the VPC (any GCP VM in the project's network) but **not** from the public internet.

### 6.4 Test the inference endpoint

**From a pod in the cluster (or a GCP VM in the same VPC):**

```bash
# List models
kubectl run -it --rm curl --image=curlimages/curl --restart=Never -- \
  http://qwen3-vllm.inference.svc.cluster.local:8000/v1/models
```

**Expected output:**
```json
{"object":"list","data":[{"id":"qwen3-32b","object":"model","created":1784284247,"owned_by":"vllm","root":"Qwen/Qwen3-32B","max_model_len":32768}]}
```

**Chat completion:**
```bash
kubectl run -it --rm curl --image=curlimages/curl --restart=Never -- \
  -X POST http://qwen3-vllm.inference.svc.cluster.local:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-32b","messages":[{"role":"user","content":"Say hello"}],"max_tokens":64}'
```

**Expected output:**
```json
{"id":"chatcmpl-...","object":"chat.completion","model":"qwen3-32b",
 "choices":[{"index":0,"message":{"role":"assistant","content":"Hello! How can I assist you today?"},"finish_reason":"stop"}],
 "usage":{"prompt_tokens":10,"total_tokens":20,"completion_tokens":10}}
```

**Success:** The inference endpoint is live and serving Qwen3-32B.

### 6.5 (Optional) Add a PodDisruptionBudget

```bash
kubectl apply -f - <<EOF
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: qwen3-vllm
  namespace: inference
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: qwen3-vllm
EOF
```

**What this does:** Protects the inference service during node upgrades/drains. GKE won't voluntarily evict the pod unless another replica is available (for a single-replica Deployment, this means drains are blocked unless you scale up first). Useful for production; optional for dev/test.

---

← Previous: **[Part 2 — GPU Node Pool & DWS](02b-gpu-nodepool-dws.md)**  |  Next: **[Part 4 — Deploy JupyterHub](02d-deploy-jupyter.md)** →

**Deployment series:** [1. Cluster Setup](02a-cluster-setup.md) → [2. GPU Node & DWS](02b-gpu-nodepool-dws.md) → **3. Inference** → [4. JupyterHub](02d-deploy-jupyter.md) → [5. Verify & Teardown](02e-verify-teardown.md)

**Related:** [Architecture Reference](01-architecture.md) · [Glossary](appendix-glossary.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Lab IaC foundation](../../lab/README.md) · [Reservations (>7 days)](../../lab/RESERVATIONS.md)
