# Remote Access — Expose JupyterHub & vLLM to the Team via HTTPS + IAP

**Audience:** The cluster admin. This guide makes the JupyterHub UI and the vLLM inference endpoint reachable by teammates who **cannot** access your GCP project or VPC directly — with no VPN and no `kubectl`.

> **New to the terminology?** IAP, Ingress, LoadBalancer, and Service are defined in the **[Glossary appendix](appendix-glossary.md)**.

## What you'll build

Two public **external HTTPS Load Balancers** (one per service, because a GKE Ingress can only target Services in its own namespace), each with a Google-managed TLS certificate:

| Service | Namespace | Public host (nip.io) | Gate |
|---|---|---|---|
| JupyterHub UI | `jupyter` | `jupyter.<JUPYTER_LB_IP>.nip.io` | **IAP** (Google sign-in, IAM-controlled) + GoogleOAuthenticator |
| vLLM inference API | `inference` | `infer.<INFER_LB_IP>.nip.io` | **API key** (`Authorization: Bearer <key>`) |

- **JupyterHub** is gated by **[Identity-Aware Proxy (IAP)](https://cloud.google.com/iap/docs/concepts-overview)**: teammates authenticate with their Google (Workspace) account and you control who gets in with a single IAM binding — no VPC access. The chart's `DummyAuthenticator` is replaced with `GoogleOAuthenticator` (restricted to your Workspace domain) so each user gets a real identity and their own home directory.
- **vLLM** is gated by a **vLLM API key** rather than IAP, so the OpenAI client works unchanged (IAP would require an OIDC-token wrapper). It's still HTTPS-only.

**Why nip.io:** Google-managed certificates require a real, publicly-resolvable domain. `nip.io` is a free wildcard-DNS service where `anything.<IP>.nip.io` resolves to `<IP>` — so you get valid managed TLS without registering a domain. If you later get a real domain, just swap the hostnames in the `ManagedCertificate` and `Ingress`/`oauth_callback_url`.

All manifests referenced below live in [`deploy/expose/`](../../deploy/expose).

---

## Prerequisites

- The stack from the [deployment series](02a-cluster-setup.md) is running (GKE cluster, GPU node, vLLM, JupyterHub on internal LBs).
- You are on a Google **Workspace / Cloud Identity** org (needed for domain-restricted access).
- `gcloud` and `kubectl` configured against the cluster (project `hdlab-elideng`, region `us-central1`).
- Roles: `roles/owner` or equivalent (to enable APIs, configure IAP/OAuth, reserve IPs, set IAM).

---

## Step 1: Enable APIs

```bash
gcloud services enable iap.googleapis.com compute.googleapis.com --project hdlab-elideng
```

---

## Step 2: Configure the OAuth consent screen (one-time)

IAP and GoogleOAuthenticator both need an **OAuth consent screen** (a "brand") on the project.

1. Console → **APIs & Services → OAuth consent screen**.
2. User type **Internal** (Workspace org — only your domain can consent). Set an app name and support email. Save.

*(This step is simplest in the console. `gcloud iap oauth-brands create` exists but is restricted; the console is the reliable path.)*

---

## Step 3: Reserve static IPs and compute your hostnames

Each Ingress needs a **global** static IP. Reserve both, then read their addresses:

```bash
gcloud compute addresses create jupyter-lb-ip --global --project hdlab-elideng
gcloud compute addresses create vllm-lb-ip    --global --project hdlab-elideng

JUPYTER_LB_IP=$(gcloud compute addresses describe jupyter-lb-ip --global --format='value(address)' --project hdlab-elideng)
INFER_LB_IP=$(gcloud compute addresses describe vllm-lb-ip    --global --format='value(address)' --project hdlab-elideng)
echo "JupyterHub host: jupyter.${JUPYTER_LB_IP}.nip.io"
echo "vLLM host:       infer.${INFER_LB_IP}.nip.io"
```

Substitute the placeholders in the manifests (and set your Workspace domain):

```bash
export WORKSPACE_DOMAIN=yourco.com     # <-- your Workspace domain

sed -i \
  -e "s/<JUPYTER_LB_IP>/${JUPYTER_LB_IP}/g" \
  -e "s/<INFER_LB_IP>/${INFER_LB_IP}/g" \
  -e "s/<WORKSPACE_DOMAIN>/${WORKSPACE_DOMAIN}/g" \
  deploy/expose/jupyter-values-iap.yaml \
  deploy/expose/jupyter-managedcert.yaml \
  deploy/expose/vllm-managedcert.yaml
```

---

## Step 4: Create OAuth clients and secrets

You need **two** OAuth 2.0 clients (both under the consent screen from Step 2):

**(a) IAP OAuth client** — used by IAP to gate JupyterHub. Console → **APIs & Services → Credentials → Create OAuth client ID → Web application**. Note the client ID/secret, then create the k8s secret:

```bash
kubectl -n jupyter create secret generic jupyter-iap-oauth \
  --from-literal=client_id=<IAP_OAUTH_CLIENT_ID> \
  --from-literal=client_secret=<IAP_OAUTH_CLIENT_SECRET>
```

**(b) JupyterHub's own OAuth client** — used by `GoogleOAuthenticator`. Create another **Web application** client. Add this **Authorized redirect URI**:

```
https://jupyter.<JUPYTER_LB_IP>.nip.io/hub/oauth_callback
```

Put its client ID/secret into `deploy/expose/jupyter-values-iap.yaml` (the `GoogleOAuthenticator` block; the `sed` in Step 3 already filled the callback URL and domain).

**(c) vLLM API key** — generate a strong key and store it:

```bash
kubectl -n inference create secret generic vllm-api-key \
  --from-literal=api-key="$(openssl rand -base64 32)"
# print it once to share with the team (store it in your secrets manager):
kubectl -n inference get secret vllm-api-key -o jsonpath='{.data.api-key}' | base64 -d; echo
```

> The `*.secret.example.yaml` files in `deploy/expose/` are templates only — create the real secrets with the commands above; don't commit real values.

---

## Step 5: Expose vLLM (API-key gated)

```bash
# Roll the API key into the running deployment (adds the VLLM_API_KEY env)
kubectl -n inference apply -f deploy/inference/vllm-deployment.yaml
kubectl -n inference rollout status deploy/qwen3-vllm

# Switch the Service from internal LB to ClusterIP+NEG, and add the Ingress stack
kubectl apply -f deploy/expose/vllm-service.yaml        # replaces vllm-service-internal
kubectl apply -f deploy/expose/vllm-backendconfig.yaml
kubectl apply -f deploy/expose/vllm-frontendconfig.yaml
kubectl apply -f deploy/expose/vllm-managedcert.yaml
kubectl apply -f deploy/expose/vllm-ingress.yaml
```

---

## Step 6: Expose JupyterHub (IAP gated)

```bash
# Apply the IAP secret + backend config + Ingress stack
kubectl apply -f deploy/expose/jupyter-iap-oauth.secret.example.yaml   # only if you didn't create it in Step 4
kubectl apply -f deploy/expose/jupyter-backendconfig.yaml
kubectl apply -f deploy/expose/jupyter-frontendconfig.yaml
kubectl apply -f deploy/expose/jupyter-managedcert.yaml

# Reconfigure JupyterHub: ClusterIP proxy + GoogleOAuthenticator (overlay on base values)
helm upgrade jhub jupyterhub/jupyterhub --namespace jupyter --version 4.4.0 \
  -f deploy/jupyter/values.yaml \
  -f deploy/expose/jupyter-values-iap.yaml \
  --timeout 10m

kubectl apply -f deploy/expose/jupyter-ingress.yaml
```

---

## Step 7: Grant the team access (IAP IAM)

Grant your team the **IAP-secured Web App User** role on the JupyterHub backend service. Simplest is a Google group:

```bash
# Find the backend service that IAP created for the Ingress
gcloud compute backend-services list --global --project hdlab-elideng

# Grant the group access to it
gcloud iap web add-iam-policy-binding \
  --resource-type=backend-services \
  --service=<JUPYTER_BACKEND_SERVICE_NAME> \
  --member="group:ai-team@${WORKSPACE_DOMAIN}" \
  --role="roles/iap.httpsResourceAccessor" \
  --project hdlab-elideng
```

Add/remove teammates by managing that group — no cluster changes needed. (The vLLM endpoint isn't IAP-gated; you control its access by who holds the API key.)

---

## Step 8: Wait, then verify

Managed certs and the LB take time to provision (**typically 10–60 minutes** the first time).

```bash
# Certs must reach Active
kubectl -n jupyter get managedcertificate jupyter-cert -o jsonpath='{.status.certificateStatus}'; echo
kubectl -n inference get managedcertificate vllm-cert   -o jsonpath='{.status.certificateStatus}'; echo

# Ingress IPs should match the reserved addresses
kubectl -n jupyter get ingress jupyter-ingress
kubectl -n inference get ingress vllm-ingress
```

**Test vLLM** (from anywhere, once the cert is Active):

```bash
curl https://infer.${INFER_LB_IP}.nip.io/v1/models \
  -H "Authorization: Bearer <THE_API_KEY>"
```

Team members use the OpenAI client unchanged:

```python
from openai import OpenAI
client = OpenAI(base_url="https://infer.<INFER_LB_IP>.nip.io/v1", api_key="<THE_API_KEY>")
print(client.chat.completions.create(
    model="qwen3-32b",
    messages=[{"role":"user","content":"hi"}], max_tokens=16).choices[0].message.content)
```

**Test JupyterHub:** browse to `https://jupyter.<JUPYTER_LB_IP>.nip.io` → Google sign-in (IAP) → JupyterHub (GoogleOAuthenticator) → profile page.

---

## Security notes & gotchas

- **Managed-cert provisioning is not instant** — it needs the Ingress live with the static IP attached and the host resolving (nip.io resolves immediately). If it's stuck `Provisioning` for >1 hour, confirm the Ingress has the reserved IP and the domain resolves to it.
- **vLLM is public with only an API key.** Rotate the key if it leaks. For stronger protection add **[Cloud Armor](https://cloud.google.com/armor)** to the vLLM backend (IP allowlist and/or rate limiting) via a `securityPolicy` in `vllm-backendconfig.yaml`.
- **JupyterHub identity trust:** IAP gates *access*; GoogleOAuthenticator establishes *identity*. Users may see two Google prompts (IAP then JupyterHub), usually seamless with an existing session. To collapse to true single sign-on you can instead trust IAP's signed `X-Goog-IAP-JWT-Assertion` header in a custom authenticator — deferred here because it needs JWT verification in the hub image.
- **The GPU node is DWS Flex-Start (7-day cap).** The LBs, IAP, and certs stay up across node rotation, but the vLLM/notebook **backends** go unavailable while the node is being replaced (see [Part 5 — node rotation](02e-verify-teardown.md#step-10-node-rotation-and-the-7-day-expiry)). Expect 502s during that window.
- **Don't leave both an internal LB and the public Ingress on the same Service** — `vllm-service.yaml` replaces `vllm-service-internal.yaml` (same name).

## Revert to internal-only

```bash
kubectl delete -f deploy/expose/jupyter-ingress.yaml -f deploy/expose/vllm-ingress.yaml
kubectl delete -f deploy/expose/jupyter-managedcert.yaml -f deploy/expose/vllm-managedcert.yaml
kubectl apply  -f deploy/inference/vllm-service-internal.yaml          # back to internal LB
helm upgrade jhub jupyterhub/jupyterhub -n jupyter --version 4.4.0 \
  -f deploy/jupyter/values.yaml --timeout 10m                          # base values (internal LB, no IAP)
gcloud compute addresses delete jupyter-lb-ip vllm-lb-ip --global --project hdlab-elideng
```

---

**Related:** [Architecture Reference](01-architecture.md) · [Deployment series](02a-cluster-setup.md) · [Inference User Guide](03-inference-endpoint-user-guide.md) · [Jupyter User Guide](04-jupyter-notebook-user-guide.md) · [Glossary](appendix-glossary.md)
