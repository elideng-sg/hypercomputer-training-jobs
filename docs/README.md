# AI Infrastructure Documentation

**Audience:** Engineers and technical staff working with GPU-powered AI infrastructure on Google Cloud Platform.

This documentation covers the complete setup, deployment, and operation of a GKE-based AI infrastructure running H100 GPUs for inference and JupyterHub notebooks.

## Documentation Guide

### Core Guides (Read in Order)

1. **[Architecture Reference](guides/01-architecture.md)** — Complete system architecture explained from first principles: GKE, DWS GPU provisioning, H100 nodes, inference endpoint, and JupyterHub. Start here to understand the system.

2. **[Deployment from Scratch](guides/02-deployment-from-scratch.md)** — Step-by-step instructions to build the entire infrastructure: VPC, GKE cluster, DWS GPU node pool, inference service, and JupyterHub deployment.

3. **[Inference Endpoint User Guide](guides/03-inference-endpoint-user-guide.md)** — How to use the vLLM-powered Qwen3-32B inference endpoint: API reference, request examples, troubleshooting.

4. **[Jupyter Notebook User Guide](guides/04-jupyter-notebook-user-guide.md)** — How to launch and use GPU-powered Jupyter notebooks: profile selection, SSH access, GPU usage, tips & limitations.

### Supporting Resources

- **[Diagrams](diagrams/)** — All architecture diagrams (SVG format): architecture overview, GKE node pools, DWS lifecycle, inference flow, Jupyter flow.

- **[Google Docs Export](export/ai-infra-guide.html)** — Self-contained HTML export combining all four guides with inlined SVG diagrams, ready to copy-paste into Google Docs.

## Quick Start

- **New to this system?** Read the [Architecture Reference](guides/01-architecture.md) first.
- **Need to deploy?** Follow the [Deployment from Scratch](guides/02-deployment-from-scratch.md) guide.
- **Using inference?** See the [Inference Endpoint User Guide](guides/03-inference-endpoint-user-guide.md).
- **Using notebooks?** See the [Jupyter Notebook User Guide](guides/04-jupyter-notebook-user-guide.md).

## Superseded Content

This documentation replaces the old `AI_INFRA_RUNBOOK.md` (removed 2026-07-17). All content has been reorganized into the structured guides above.
