# TCPXO Migration — `us-central1` → `asia-southeast1-c`

Performed **2026-08-05**. Moves the two real workloads (JupyterHub + qwen3-vllm) onto the
A3 Mega pool that has the **GPUDirect-TCPXO** fabric, releases every other A3 pool, and
migrates the team's IAP SSH access to the new nodes.

| | Before | After |
|---|---|---|
| Cluster | `hypercomputer-a3-cluster` (+3 others) | `hypercomputer-a3-tcpxo` |
| Zone | `us-central1` / `asia-east1-c` / `asia-southeast1-c` | `asia-southeast1-c` only |
| Pool | `a3-h100-dws-pool`, `a3-high-flex-pool`, `a3-tcpx-flex-pool`, `a3-mega-flex-pool` | `a3-mega-tcpxo-flex-pool` |
| Machine | `a3-highgpu-8g` / `a3-megagpu-8g` | `a3-megagpu-8g` |
| Accelerator label | `nvidia-h100-80gb` | **`nvidia-h100-mega-80gb`** |
| GPUs live | 72 across 9 nodes | 24 across 3 nodes |
| Fabric | none (or TCPX) | **TCPXO**, armed and verified |

**What deliberately did NOT change:** both team URLs (the reserved global IPs were reused),
the OAuth client and callback URL, the vLLM API key (the secret was copied, not
regenerated), and every user's Jupyter home directory (the same zonal PDs were re-attached,
not copied). No teammate has to change a bookmark, a `base_url`, or a key.

## Apply order

Order matters for 01 and 04; the rest is flexible.

| File | What it does | Notes |
|---|---|---|
| `01-static-pvs.yaml` | Re-attaches the 5 existing disks as static PVs + PVCs | **First.** `claimRef` pins each PV to exactly one PVC so user homes cannot cross-bind |
| `02-vllm-tcpxo.yaml` | qwen3-vllm, TCPXO-armed | `strategy: Recreate` — `hf-cache` is RWO |
| `03-jupyter-values-tcpxo.yaml` | z2jh 4.4.0 Helm values | Adds the 8-GPU TCPXO profile. ⚠️ the OAuth secret is **redacted** — you must pass `--set-string`, see below |
| `04-jupyter-ingress.yaml` | Cert + Ingress on the reused `jupyter-lb-ip` | **The old Ingress must be deleted first** — a global static IP binds one forwarding rule at a time, so this is an unavoidable brief outage |
| `05-holder-partial.yaml` | 6-GPU holder sharing vLLM's node | Keeps the pool at 24/24 held |
| `06-vllm-ingress.yaml` | Restores the public vLLM endpoint on the reused `vllm-lb-ip` | Was found **already dead** (`http=000`) during the audit — rebuilt rather than dropped from the docs |

```bash
gcloud container clusters get-credentials hypercomputer-a3-tcpxo \
  --location asia-southeast1-c --project hdlab-elideng

kubectl apply -f 01-static-pvs.yaml
kubectl apply -f 02-vllm-tcpxo.yaml
# The OAuth client_secret is redacted from the values file (public repo) -- pass it here.
# It is still live in the cluster, so you can read it back rather than hunting for it.
# z2jh keeps it inside the `values.yaml` key of secret/hub, not as a flat key:
SECRET=$(kubectl get secret hub -n jupyter -o jsonpath='{.data.values\.yaml}' \
  | base64 -d | awk '/client_secret:/{print $2; exit}')
helm upgrade --install jhub jupyterhub/jupyterhub -n jupyter --version 4.4.0 \
  -f 03-jupyter-values-tcpxo.yaml --timeout 15m \
  --set-string hub.config.GoogleOAuthenticator.client_secret="$SECRET"
kubectl apply -f 04-jupyter-ingress.yaml
kubectl apply -f 05-holder-partial.yaml
kubectl apply -f 06-vllm-ingress.yaml
deploy/ops/grant-node-ssh.sh          # instance-level SSH IAM for the team
```

## Verified after migration

- vLLM `2/2 Running, 0 restarts`; `/v1/models` returns `qwen3-32b` and a chat completion
  really generates tokens
- The **public** vLLM endpoint works end-to-end on the unchanged URL: `vllm-cert` reached
  `Active`, `https://infer.136.69.110.10.nip.io/v1/models` returns **200** with the API key,
  **401** without it, and plain HTTP **301**-redirects to HTTPS. (This URL answered `http=000`
  before the migration — it had been dead, not merely moved.)
- JupyterHub HTTPS returns 200 on the unchanged URL; `jupyter-cert` Active; hub DB re-attached
- Fabric genuinely armed, not merely configured: 9 NICs (`eth0`+`eth1..eth8`),
  `/dev/aperture_devices` populated with 8 GPU-NIC BDFs, FasTrak plugin present, rxdm logging
  "Entering the event loop", 0 sidecar restarts
- 24/24 GPUs held throughout — no node was ever left idle and unheld
- Team SSH IAM (`osAdminLogin` + `compute.viewer`) present on all 3 nodes

## Two known gaps (need an owner decision — both widen access project-wide)

**1. Nobody holds `roles/iap.tunnelResourceAccessor`**, so IAP SSH tunnels cannot open
regardless of the node-level grants.

**2. OS Login is not enabled** (no `enable-oslogin` metadata at project or node level), so
`roles/compute.osAdminLogin` currently has nothing to act on.

Both are project-level changes affecting every VM in the project, so they were left for an
owner rather than applied unilaterally. Commands and trade-offs:
[Remote Access → Part C](../../docs/guides/05-remote-access-iap.md#-two-prerequisites-are-currently-not-satisfied).

## Other follow-ups

- **The OAuth `client_secret` is redacted from `03-jupyter-values-tcpxo.yaml`, so that file
  alone will not deploy.** This repository is **public**, so the live secret was deliberately
  not committed. Supply it with `--set-string` at install time — the exact command is in that
  file's header. The secret is still applied in the running cluster, so sign-in works today;
  only the repo copy is redacted. **The value was briefly held in a plaintext working file
  during the migration, so rotating the OAuth client secret is still the safe call.**
- **Instance-level SSH IAM does not survive node rotation.** Re-run
  [`../ops/grant-node-ssh.sh`](../ops/grant-node-ssh.sh) after any Flex rotation or pool
  recreate. Mitigated, not eliminated.
- The **capacity watchdog** (`gpu-flex-watchdog.sh`) was swapped to protect this pool and drop
  the released ones, and a latent node-counting bug was fixed — GKE truncates long pool names
  in node names, so the old substring filter matched **zero** nodes for
  `a3-mega-tcpxo-flex-pool`, which reads as "LAPSED" and would have **deleted and recreated a
  perfectly healthy pool**, destroying the workloads on it. It now counts by the
  `goog-k8s-node-pool-name` label. The script is **deployed from GCS, not this repo**:
  `gcloud storage cp gpu-flex-watchdog.sh gs://hdlab-elideng-gpu-watchdog/watchdog.sh`.
- Source cluster `hypercomputer-a3-asiasoutheast1` still holds scaled-to-0 Deployments and 5
  `Released` PVs. Left in place deliberately as a rollback path; delete once confident.
- `a3-mega-cal-pool` (us-central1, 0 nodes) was **not** released — it is pinned to reservation
  `frm16-20260906` and is where the approved September calendar bookings land. The us-central1
  zone holders were scaled to 0 rather than deleted for the same reason.

## Background

The fabric arming here is copied from the validated 317.84 GB/s pod spec in
`hypercomputer-internode-deepdive` (`manifests/tcpxo/workbench-tcpxo.yaml`). The five
quiet-failure modes and the NCCL "contract" (14 policy-enforced variables that hang init on
mismatch) are documented in
[Architecture §6](../../docs/guides/01-architecture.md#6-the-tcpxo-fabric).
