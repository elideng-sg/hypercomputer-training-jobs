# deploy/expose — public remote access (HTTPS + IAP)

Manifests that expose JupyterHub and the vLLM endpoint to teammates outside the
VPC, via external HTTPS Load Balancers with Google-managed TLS.

- **JupyterHub** (`jupyter-*`): gated by **Identity-Aware Proxy** (Google sign-in,
  IAM-controlled) + `GoogleOAuthenticator`.
- **vLLM** (`vllm-*`): gated by an **API key** (`Authorization: Bearer <key>`), so
  the OpenAI client works unchanged.

Two Ingresses / two static IPs (a GKE Ingress can't route across namespaces).

**Do not apply these blindly** — they contain `<PLACEHOLDER>` values (LB IPs,
Workspace domain) and reference secrets you must create out-of-band. The
`*.secret.example.yaml` files are templates only.

**Follow the runbook:** [`docs/guides/05-remote-access-iap.md`](../../docs/guides/05-remote-access-iap.md).
