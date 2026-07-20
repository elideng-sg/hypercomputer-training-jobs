# AI Infrastructure Documentation

**Audience:** Engineers and technical staff working with GPU-powered AI infrastructure on Google Cloud Platform.

This documentation covers the complete setup, deployment, and operation of a GKE-based AI infrastructure running H100 GPUs for inference and JupyterHub notebooks. It is organized as small, topic-scoped guides (rather than one long document), with a shared glossary appendix and auto-laid-out architecture diagrams.

## Documentation Guide

### 1. Understand the system

- **[Architecture Reference](guides/01-architecture.md)** — The system explained top-down: high-level GKE architecture, the layered stack, how the scarce GPU node is obtained via DWS, and how the inference and notebook workloads share the GPUs. **Start here.**

### 2. Deploy it from scratch (five-part series, in order)

1. **[Part 1 — Cluster Setup](guides/02a-cluster-setup.md)** — Project setup, GPU quota, and the regional GKE cluster.
2. **[Part 2 — GPU Node Pool & DWS](guides/02b-gpu-nodepool-dws.md)** — The A3 node pool, DWS provisioning, namespaces, and storage.
3. **[Part 3 — Deploy Inference](guides/02c-deploy-inference.md)** — vLLM serving Qwen3-32B.
4. **[Part 4 — Deploy JupyterHub](guides/02d-deploy-jupyter.md)** — GPU-enabled notebooks.
5. **[Part 5 — Verify & Teardown](guides/02e-verify-teardown.md)** — End-to-end checks, node rotation, the 7-day expiry, and cleanup.

### 3. Use it

- **[Inference Endpoint User Guide](guides/03-inference-endpoint-user-guide.md)** — Use the vLLM-powered Qwen3-32B endpoint: API reference, request examples, troubleshooting.
- **[Jupyter Notebook User Guide](guides/04-jupyter-notebook-user-guide.md)** — Launch and use GPU-powered Jupyter notebooks: profile selection, GPU usage, calling the inference endpoint, tips & limitations.

### 4. Share it with a team

- **[Remote Access — HTTPS + IAP](guides/05-remote-access-iap.md)** — Expose JupyterHub and the vLLM endpoint to teammates who can't reach your VPC directly: external HTTPS load balancers, Identity-Aware Proxy (Google sign-in) for the notebook UI, and an API key for the inference endpoint. No VPN, no `kubectl` for users.

### Reference

- **[Glossary appendix](guides/appendix-glossary.md)** — Plain-language definitions of every term (GPU, GKE, pod, DWS, tensor parallelism, …). The guides link here on first use of each term.

## Supporting Resources

- **[Diagrams](diagrams/)** — Architecture diagrams in SVG. Each diagram is generated from a Graphviz source in [`diagrams/src/`](diagrams/src/) (`dot -Tsvg`), so auto-layout keeps boxes non-overlapping and arrows cleanly routed. Edit the `.dot` source, not the SVG.

- **[Google Docs Exports](export/)** — One self-contained HTML file per topic guide, with inlined SVG diagrams and working cross-links, ready to copy-paste into Google Docs. Regenerate with:

  ```bash
  scripts/build_docs_export.sh
  ```

  (Requires `graphviz` and `python3`. The script renders the diagrams from their `.dot` sources, then exports each guide via `scripts/export_to_google_docs.py`.)

## Quick Start

- **New to this system?** Read the [Architecture Reference](guides/01-architecture.md) first.
- **Need to deploy?** Start the [deployment series at Part 1](guides/02a-cluster-setup.md).
- **Using inference?** See the [Inference Endpoint User Guide](guides/03-inference-endpoint-user-guide.md).
- **Using notebooks?** See the [Jupyter Notebook User Guide](guides/04-jupyter-notebook-user-guide.md).
- **Confused by a term?** Check the [Glossary appendix](guides/appendix-glossary.md).

## Superseded Content

This documentation replaces the old `AI_INFRA_RUNBOOK.md` (removed 2026-07-17) and the earlier single-file `export/ai-infra-guide.html` (split into per-topic exports on 2026-07-20). All content has been reorganized into the structured guides above.
