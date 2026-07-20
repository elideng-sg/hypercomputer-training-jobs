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

**Why not IAP?** The IAP OAuth Admin APIs were shut down in early 2026, so the "bring-your-own OAuth client for IAP" path is no longer usable. GoogleOAuthenticator uses a *standard* OAuth 2.0 client (unaffected) and gives equivalent domain-restricted access control. If you later want IAP as an extra edge layer, enable it via Google-managed OAuth in the console.

**Why nip.io:** Google-managed certs need a publicly-resolvable domain. `nip.io` resolves `anything.<IP>.nip.io` → `<IP>`, giving valid managed TLS with no domain registration. Swap in a real domain later by editing the `ManagedCertificate`, `Ingress`, and `oauth_callback_url`.

Manifests live in [`deploy/expose/`](../../deploy/expose).

> **Current live deployment (project `hdlab-elideng`, region `us-central1`):**
> - vLLM: **`https://infer.136.69.110.10.nip.io`** (Part A below — already provisioned)
> - JupyterHub: **`https://jupyter.34.54.187.199.nip.io`** (Part B — awaiting the OAuth client step)
> - The vLLM API key lives in the `vllm-api-key` secret; retrieve it with:
>   ```bash
>   kubectl -n inference get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d; echo
>   ```
> Certs can take 10–60 min to go **Active** after first provisioning.

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

### B5. Verify

```bash
kubectl -n jupyter get managedcertificate jupyter-cert -o jsonpath='{.status.certificateStatus}'; echo
kubectl -n jupyter get ingress jupyter-ingress
```

Browse to `https://jupyter.<JUPYTER_LB_IP>.nip.io` → "Sign in with Google" → only `@<WORKSPACE_DOMAIN>` accounts are allowed → profile page.

### Managing who has access

Access is anyone in `hosted_domain`. To restrict further, set `allow_all: false` and list `allowed_users` in `jupyter-values-public.yaml`, then re-run the `helm upgrade`.

---

## Security notes & gotchas

- **Managed-cert provisioning isn't instant** (10–60 min). It needs the Ingress live with the static IP attached; nip.io resolves immediately. If stuck `Provisioning` >1 hour, confirm the Ingress has the reserved IP.
- **vLLM is public with only an API key.** Rotate it if it leaks (`kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -`, then `kubectl rollout restart deploy/qwen3-vllm`). For stronger protection add **[Cloud Armor](https://cloud.google.com/armor)** (IP allowlist / rate limiting) to `vllm-backendconfig.yaml` via a `securityPolicy`.
- **JupyterHub has no IAP layer** — the gate is GoogleOAuthenticator's `hosted_domain`. That is real, domain-restricted auth; just be sure `hosted_domain` is set so it's not open to any Google account.
- **The GPU node is DWS Flex-Start (7-day cap).** LBs and certs stay up across node rotation, but the vLLM/notebook **backends** go unavailable while the node is replaced (see [Part 5 — node rotation](02e-verify-teardown.md#step-10-node-rotation-and-the-7-day-expiry)) — expect 502s during that window.

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

**Related:** [Architecture Reference](01-architecture.md) · [Deployment series](02a-cluster-setup.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Glossary](appendix-glossary.md)
