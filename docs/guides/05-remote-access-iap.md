# Remote Access — Expose JupyterHub & vLLM to the Team over HTTPS

**Audience:** The cluster admin. This makes the JupyterHub UI and the vLLM inference endpoint reachable by teammates who **cannot** access your GCP project or VPC directly — no VPN, no `kubectl`.

> **New to the terminology?** Ingress, LoadBalancer, and Service are defined in the **[Glossary appendix](appendix-glossary.md)**.

## Approach

Two public **external HTTPS Load Balancers** (one per service — a GKE Ingress can only target Services in its own namespace), each with a Google-managed TLS certificate:

| Service | Public host | Gate |
|---|---|---|
| vLLM inference API | `infer.<VLLM_LB_IP>.nip.io` | **API key** (`Authorization: Bearer <key>`) |
| JupyterHub UI | `jupyter.<JUPYTER_LB_IP>.nip.io` | **GoogleOAuthenticator**, restricted to your Workspace domain |

- **vLLM** is gated by a **vLLM API key** so the OpenAI client works unchanged.
- **JupyterHub** is gated by **GoogleOAuthenticator** (Google sign-in restricted to your Workspace domain), replacing the demo `DummyAuthenticator`. Each user gets a real identity and their own home directory.

**Why not IAP for the web UIs?** The IAP OAuth Admin APIs were shut down in early 2026, so the "bring-your-own OAuth client for IAP" path is no longer usable for *web* access. GoogleOAuthenticator uses a *standard* OAuth 2.0 client (unaffected) and gives equivalent domain-restricted access control. If you later want IAP as an extra edge layer, enable it via Google-managed OAuth in the console.

> **IAP is still used — for SSH.** That shutdown affected IAP's *web/HTTPS* OAuth path only.
> **IAP TCP forwarding**, which is how admins get a shell on the GPU nodes without public
> IPs or a VPN, is unaffected and is how node access works today. See
> **[Part C — SSH to the GPU nodes over IAP](#part-c--ssh-to-the-gpu-nodes-over-iap)**.

**Why nip.io:** Google-managed certs need a publicly-resolvable domain. `nip.io` resolves `anything.<IP>.nip.io` → `<IP>`, giving valid managed TLS with no domain registration. Swap in a real domain later by editing the `ManagedCertificate`, `Ingress`, and `oauth_callback_url`.

Manifests live in [`deploy/expose/`](../../deploy/expose).

> **Current live deployment (project `hdlab-elideng`, cluster `hypercomputer-a3-tcpxo`, zone `asia-southeast1-c`):**
> - vLLM: **`https://infer.136.69.110.10.nip.io`**
> - JupyterHub: **`https://jupyter.34.54.187.199.nip.io`**
> - Admin SSH to GPU nodes: **IAP TCP forwarding** ([Part C](#part-c--ssh-to-the-gpu-nodes-over-iap))
> - The vLLM API key lives in the `vllm-api-key` secret; retrieve it with:
>   ```bash
>   kubectl -n inference get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d; echo
>   ```
> Certs can take 10–60 min to go **Active** after first provisioning.
>
> **Migrated 2026-08-05 (`us-central1` → `asia-southeast1-c`).** Both URLs and the API key
> are **unchanged** — the same reserved global IPs were reused and the key secret was copied
> rather than regenerated, so no teammate has to update a `base_url` or re-fetch a key.
> First get credentials for the new cluster before running any `kubectl` below:
> ```bash
> gcloud container clusters get-credentials hypercomputer-a3-tcpxo \
>   --location asia-southeast1-c --project hdlab-elideng
> ```

---

## Prerequisites

- The stack from the [deployment series](02a-cluster-setup.md) is running.
- You are on a Google **Workspace / Cloud Identity** org.
- `gcloud`, `kubectl`, `helm` configured against the cluster.
- Roles to enable APIs, reserve IPs, configure OAuth, and run Helm.

## One-time setup (both services)

```bash
gcloud services enable compute.googleapis.com container.googleapis.com --project hdlab-elideng

# Reserve one global static IP per service, then read the addresses:
gcloud compute addresses create vllm-lb-ip    --global --project hdlab-elideng
gcloud compute addresses create jupyter-lb-ip --global --project hdlab-elideng
VLLM_IP=$(gcloud compute addresses describe vllm-lb-ip    --global --format='value(address)' --project hdlab-elideng)
JUP_IP=$(gcloud compute addresses describe jupyter-lb-ip  --global --format='value(address)' --project hdlab-elideng)
echo "vLLM host:  infer.${VLLM_IP}.nip.io"
echo "Jupyter host: jupyter.${JUP_IP}.nip.io"
```

---

## Part A — vLLM public endpoint (API-key gated)

```bash
# 1. Create a strong API key secret
kubectl -n inference create secret generic vllm-api-key \
  --from-literal=api-key="$(openssl rand -hex 24)"

# 2. Add the key to the running deployment (vLLM then requires it on EVERY request,
#    in-cluster and external). Re-apply the manifest, or patch in place:
kubectl -n inference patch deploy qwen3-vllm --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"VLLM_API_KEY","valueFrom":{"secretKeyRef":{"name":"vllm-api-key","key":"api-key"}}}}]'
kubectl -n inference rollout status deploy/qwen3-vllm

# 3. Swap the internal LB for a ClusterIP+NEG Service and add the HTTPS Ingress
kubectl apply -f deploy/expose/vllm-service.yaml          # replaces vllm-service-internal
kubectl apply -f deploy/expose/vllm-backendconfig.yaml
kubectl apply -f deploy/expose/vllm-frontendconfig.yaml
sed "s/<INFER_LB_IP>/${VLLM_IP}/g" deploy/expose/vllm-managedcert.yaml | kubectl apply -f -
kubectl apply -f deploy/expose/vllm-ingress.yaml
```

**Verify** (once the cert is Active):

```bash
kubectl -n inference get managedcertificate vllm-cert -o jsonpath='{.status.certificateStatus}'; echo
KEY=$(kubectl -n inference get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d)
curl -s https://infer.${VLLM_IP}.nip.io/v1/models -H "Authorization: Bearer $KEY"
```

**Share the key with the team** over a secure channel (a password manager — not chat or email). Team members then set `export VLLM_API_KEY=<key>` and use it as shown in the [Inference Endpoint User Guide → Getting your API key](03-inference-endpoint-user-guide.md#getting-your-api-key).

> ⚠️ **Enabling the key changes existing behavior:** in-cluster callers (JupyterHub notebooks) that previously used `api_key="none"` now get **401** and must send the real key. The user guides have been updated accordingly.

---

## Part B — JupyterHub public UI (Google sign-in)

### B1. OAuth consent screen (one-time, console)

Console → **APIs & Services → OAuth consent screen** → User type **Internal** (your Workspace org) → set app name + support email → Save.

### B2. Create an OAuth 2.0 Web client (console)

Console → **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application**. Add this **Authorized redirect URI** (use your reserved Jupyter IP):

```
https://jupyter.<JUPYTER_LB_IP>.nip.io/hub/oauth_callback
```

Note the generated **Client ID** and **Client secret**.

### B3. Fill in the values overlay

```bash
export WORKSPACE_DOMAIN=yourco.com     # your Workspace domain
sed -i \
  -e "s/<JUPYTER_LB_IP>/${JUP_IP}/g" \
  -e "s/<WORKSPACE_DOMAIN>/${WORKSPACE_DOMAIN}/g" \
  deploy/expose/jupyter-values-public.yaml deploy/expose/jupyter-managedcert.yaml
```

Then edit `deploy/expose/jupyter-values-public.yaml` and paste the **Client ID / Client secret** from B2 into the `GoogleOAuthenticator` block. (For real deployments, prefer a Kubernetes secret / `--set` over committing them.)

### B4. Apply

```bash
kubectl apply -f deploy/expose/jupyter-backendconfig.yaml
kubectl apply -f deploy/expose/jupyter-frontendconfig.yaml
kubectl apply -f deploy/expose/jupyter-managedcert.yaml

# Reconfigure JupyterHub: ClusterIP proxy + GoogleOAuthenticator (overlay on base values)
helm upgrade jhub jupyterhub/jupyterhub --namespace jupyter --version 4.4.0 \
  -f deploy/jupyter/values.yaml \
  -f deploy/expose/jupyter-values-public.yaml \
  --timeout 10m

kubectl apply -f deploy/expose/jupyter-ingress.yaml
```

> **For the current TCPXO deployment, this whole overlay is already merged into one file.**
> Use it instead of the two-file overlay above — it carries the OAuth config, the Mega
> accelerator label, and the TCPXO-armed 8-GPU profile together:
>
> ```bash
> helm upgrade --install jhub jupyterhub/jupyterhub --namespace jupyter --version 4.4.0 \
>   -f deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml --timeout 15m
> kubectl apply -f deploy/tcpxo-migration/04-jupyter-ingress.yaml
> ```
>
> **The static IP can only serve one Ingress at a time.** A global static IP binds to exactly
> one forwarding rule, so if an Ingress in another cluster still holds `jupyter-lb-ip`, the
> new one stays `ADDRESS`-less until the old one is deleted. That delete-then-create window
> is an unavoidable brief outage on the team URL — it is why the URL is preserved rather than
> reissued.

### B5. Verify

```bash
kubectl -n jupyter get managedcertificate jupyter-cert -o jsonpath='{.status.certificateStatus}'; echo
kubectl -n jupyter get ingress jupyter-ingress
```

Browse to `https://jupyter.<JUPYTER_LB_IP>.nip.io` → "Sign in with Google" → only `@<WORKSPACE_DOMAIN>` accounts are allowed → profile page.

### Managing who has access

Access is anyone in `hosted_domain`. To restrict further, set `allow_all: false` and list `allowed_users` in `jupyter-values-public.yaml`, then re-run the `helm upgrade`.

---

## Part C — SSH to the GPU nodes over IAP

**Audience:** admins and ML engineers who need a real shell *on the node* — to run
`nvidia-smi`, inspect `/dev/aperture_devices`, read rxdm logs, or debug the fabric.
Notebook and inference users do **not** need this.

The GPU nodes have **no external IP**. Access goes through **IAP TCP forwarding**: `gcloud`
opens a tunnel from Google's IAP range (`35.235.240.0/20`) to port 22 on the node, so
nothing is exposed to the internet and no VPN is required.

### Connecting

```bash
# List the current GPU nodes (names change on every rotation — never hard-code one)
gcloud compute instances list --project hdlab-elideng \
  --filter="labels.goog-k8s-node-pool-name=a3-mega-tcpxo-flex-pool" \
  --format="table(name,status,creationTimestamp)"

# SSH via the IAP tunnel
gcloud compute ssh <node-name> \
  --zone asia-southeast1-c \
  --project hdlab-elideng \
  --tunnel-through-iap
```

Useful once you are on a node:

```bash
nvidia-smi                       # GPU health and who is using what
ls /sys/class/net                # expect eth0..eth8 — 9 NICs means the fabric is plumbed
ls /dev/aperture_devices         # expect 8 GPU-NIC BDFs
```

### Granting a teammate access

Access is **instance-level** IAM, so it must be re-applied after every node rotation:

```bash
deploy/ops/grant-node-ssh.sh                          # defaults to the TCPXO pool
CLUSTER=... ZONE=... POOL=... deploy/ops/grant-node-ssh.sh   # any other pool
```

It applies, per node:

| What | Attached to | Why |
|---|---|---|
| `enable-oslogin=TRUE` (**metadata**) | the instance | makes `osAdminLogin` effective at all — without it guests fail on `setMetadata` |
| `roles/compute.osAdminLogin` | the instance | login **+ sudo** on the node |
| `roles/compute.viewer` | the instance | lets `gcloud compute ssh` resolve the instance name (skipped for the project owner, who already has it) |
| `roles/iap.tunnelResourceAccessor` | the instance's **IAP tunnel** resource | lets the tunnel open at all |

Those are the four **instance-scoped** things — the ones a rotation destroys. The other two of
the [six a guest needs](#the-five-bindings-a-guest-actually-needs) are project- or
service-account-scoped, already in place for this team, and survive rotation, so the script
deliberately leaves them alone.

> ⚠️ **Instance-level IAM and metadata do not survive node replacement.** Flex-Start nodes are
> replaced at the 7-day boundary, on preemption, and on every pool recreate the
> [capacity watchdog](01-architecture.md#6b-capacity-watchdog) performs. A new node is a new
> IAM resource with an **empty** policy and no `enable-oslogin` key, so the team silently
> loses SSH with no error anywhere until someone tries to connect.
> **Re-run `grant-node-ssh.sh` after any rotation** — and remember the owner will not notice
> the breakage, because an owner can write metadata and so still gets in.

**Why not just grant at project level?** `roles/compute.osAdminLogin` project-wide would
survive rotation, but it grants root-equivalent login on *every* VM in the project. The
instance-level grant plus a re-arm script keeps the blast radius at the GPU nodes the team
is meant to be using. That is a deliberate trade of convenience for scope — if you would
rather have durability, the project-level grant is one command and the script becomes
unnecessary.

### The five bindings a guest actually needs

...plus one metadata key, so **six** things in total. The original five are IAM; #6 is not,
which is exactly why it was missed.

A **project owner** needs none of them: `roles/owner` satisfies all six. That asymmetry is
the whole reason this is fiddly — *your* working SSH tells you nothing about whether a
teammate can connect, and it is how #6 stayed hidden until Alex hit it on 2026-08-05. Listed
in the order `gcloud compute ssh` evaluates them, because each failure masks the ones after
it:

| # | Binding | Attach to | Error if missing |
|---|---|---|---|
| 1 | custom `sshResolveMinimal` (`compute.projects.get` only) | **project** | `Required 'compute.projects.get' permission` |
| 2 | `roles/compute.viewer` | the instance | `Required 'compute.instances.get' permission` |
| 3 | `roles/iap.tunnelResourceAccessor` | **the IAP tunnel resource** for that zone+instance | `Error while connecting [4033: 'not authorized']` |
| 4 | `roles/compute.osAdminLogin` | the instance | `Permission denied (publickey)` |
| 5 | `roles/iam.serviceAccountUser` | the node's attached SA (`151935633952-compute@developer.gserviceaccount.com`) | `Permission denied (publickey)` — identical to #4 |
| 6 | `enable-oslogin=TRUE` — **metadata, not IAM** | the instance | `Required 'compute.instances.setMetadata' permission` |

`grant-node-ssh.sh` applies **#2, #3, #4 and #6** — everything a rotation destroys. #1 and #5
are project- and service-account-scoped, are already in place for the team, and survive
rotation.

#6 is not an IAM binding at all, which is why it is easy to miss and why it is listed last
despite being checked first in practice — see [below](#binding-6-enable-oslogintrue-metadata-not-iam).

> ⚠️ **The tunnel grant must be per-instance, and `gcloud` has no convenient command for
> it.** IAP tunnel permissions live in a **separate resource hierarchy** from Compute, so a
> *project-level* grant of `roles/iap.tunnelResourceAccessor` is **never consulted and
> silently does nothing**. This also means checking project IAM proves nothing:
> `gcloud projects get-iam-policy … --filter='bindings.role=roles/iap.tunnelResourceAccessor'`
> returns empty even when tunnels work. Use the REST API:

```bash
PN=151935633952; Z=asia-southeast1-c; TOKEN=$(gcloud auth print-access-token)

# Read (an empty policy comes back as just {"etag":"ACAB"})
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://iap.googleapis.com/v1/projects/$PN/iap_tunnel/zones/$Z/instances/<NODE>:getIamPolicy" -d '{}'

# Grant (replaces the policy — include every member you want to keep)
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "https://iap.googleapis.com/v1/projects/$PN/iap_tunnel/zones/$Z/instances/<NODE>:setIamPolicy" \
  -d '{"policy":{"bindings":[{"role":"roles/iap.tunnelResourceAccessor",
       "members":["user:someone@google.com"]}]}}'
```

Trap on #1: it **must** be project-level. IAM flows downward only (project → zone →
instance), so an instance binding can never satisfy a project-level check even when the role
contains the permission. `roles/browser` is the wrong fix — it carries
`resourcemanager.projects.get`, a *different* permission.

### Binding #6: `enable-oslogin=TRUE` (metadata, not IAM)

`roles/compute.osAdminLogin` is an **OS Login** role. If OS Login is not switched on for the
node, the role has nothing to act on and `gcloud` silently falls back to **writing an SSH key
into instance/project metadata** — which needs `compute.instances.setMetadata`, a permission
guests do not and should not have. A teammate gets:

```
Updating project ssh metadata...failed.
Updating instance ssh metadata...failed.
ERROR: (gcloud.compute.ssh) Could not add SSH key to instance metadata ...
 - Required 'compute.instances.setMetadata' permission for '...instances/<node>'
```

**COS does not enable OS Login by itself** — an earlier version of this guide claimed it did,
which was wrong. The key was absent from project metadata and from every node, and
`constraints/compute.requireOsLogin` is not enforced. The reason it looked enabled is a trap
worth knowing: the owner's node username, `ext_elideng_google_com`, is just this
workstation's **local** username, and there was a **metadata SSH key under exactly that
name** — so the owner had been using the metadata path all along while appearing to use OS
Login. Testing as an owner exercises the fallback, not the path the team uses.

Set it **per instance** — `grant-node-ssh.sh` now does this automatically:

```bash
gcloud compute instances add-metadata <node> \
  --project hdlab-elideng --zone asia-southeast1-c \
  --metadata enable-oslogin=TRUE
```

Two things **not** to do:

- **Not project-wide.** It changes SSH authentication on every VM in the project (including
  `ubuntu-secure-desktop`), and this project *does* have project-level `ssh-keys` metadata in
  use — anyone depending on it elsewhere could lose access.
- **Not via node-pool metadata.** Changing a pool's metadata recreates its nodes, and these
  are scarce Flex-Start A3 Mega nodes that may not come back at all.

Setting it on a running instance needs no reboot and does not disturb workloads (verified:
all pods stayed `Running`, all nodes `Ready`). Because it is instance metadata, it **dies
with the node** — same rotation problem as bindings #2–#4.

**Verified with a non-owner.** A throwaway service account holding exactly the five bindings
and **no** `setMetadata` permission connected successfully, landing as a real OS Login
account (`sa_1042826306…`, uid `2779788392`) with no metadata write attempted. The probe and
all of its bindings were removed afterwards.

**Also satisfied:** the firewall rule `allow-ssh-from-iap`
(`35.235.240.0/20` → `tcp:22`, network `default`, enabled) covers the nodes — their `eth0`
is on `default`. The 8 fabric NICs are on separate VPCs and carry no SSH.

### Team-facing runbook

The team's copy-paste instructions live in a Google Doc, **GPU Node Access — A3 Mega H100
with TCPXO fabric**, shared to named individuals (not the Cloud audience). Because it names
specific instances, **it goes stale on every node rotation** — a teammate's old command then
fails with `The resource ... was not found`, which reads like a permissions problem but means
the machine is gone. Update it whenever nodes rotate.

---

## Security notes & gotchas

- **Managed-cert provisioning isn't instant** (10–60 min). It needs the Ingress live with the static IP attached; nip.io resolves immediately. If stuck `Provisioning` >1 hour, confirm the Ingress has the reserved IP.
- **vLLM is public with only an API key.** Rotate it if it leaks (`kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`, then `kubectl rollout restart deploy/qwen3-vllm`). For stronger protection add **[Cloud Armor](https://cloud.google.com/armor)** (IP allowlist / rate limiting) to `vllm-backendconfig.yaml` via a `securityPolicy`.
- **JupyterHub has no IAP layer** — the gate is GoogleOAuthenticator's `hosted_domain`. That is real, domain-restricted auth; just be sure `hosted_domain` is set so it's not open to any Google account.
- **The GPU nodes are Flex-Start (7-day cap).** LBs and certs stay up across node rotation, but the vLLM/notebook **backends** go unavailable while a node is replaced (see [Part 5 — node rotation](02e-verify-teardown.md#step-10-node-rotation-and-the-7-day-expiry)) — expect 502s during that window. The [capacity watchdog](01-architecture.md#6b-capacity-watchdog) re-grabs the pool automatically, but **SSH access must be re-granted by hand** ([Part C](#part-c--ssh-to-the-gpu-nodes-over-iap)).
- **The OAuth client secret is not in this repo — keep it that way.** **This repository is public.** `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` ships `client_secret: ""` and the real value is passed at install time with `--set-string hub.config.GoogleOAuthenticator.client_secret=...`. The live value is readable from the cluster (note it sits inside the `values.yaml` key of `secret/hub`, not as a flat key):
  ```bash
  kubectl get secret hub -n jupyter -o jsonpath='{.data.values\.yaml}' \
    | base64 -d | awk '/client_secret:/{print $2; exit}'
  ```
  It did sit in a plaintext working file during the migration, so **rotating the client secret is still the safe call.**

## Revert to internal-only

```bash
kubectl delete -f deploy/expose/jupyter-ingress.yaml -f deploy/expose/vllm-ingress.yaml
kubectl delete -f deploy/expose/jupyter-managedcert.yaml
kubectl delete managedcertificate vllm-cert -n inference
kubectl apply  -f deploy/inference/vllm-service-internal.yaml
helm upgrade jhub jupyterhub/jupyterhub -n jupyter --version 4.4.0 -f deploy/jupyter/values.yaml --timeout 10m
gcloud compute addresses delete jupyter-lb-ip vllm-lb-ip --global --project hdlab-elideng
```

---

**Document version:** 2026-08-05 — added Part C (SSH over IAP) and migrated all cluster/zone references to `hypercomputer-a3-tcpxo` / `asia-southeast1-c`.

**Related:** [Architecture Reference](01-architecture.md) · [Deployment series](02a-cluster-setup.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Glossary](appendix-glossary.md)
