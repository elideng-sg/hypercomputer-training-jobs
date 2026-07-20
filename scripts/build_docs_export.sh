#!/usr/bin/env bash
#
# Build the documentation exports:
#   1. Render architecture diagrams from Graphviz sources (docs/diagrams/src/*.dot)
#      to SVG (docs/diagrams/*.svg) using `dot` (auto-layout: no overlapping boxes,
#      routed edges).
#   2. Export one self-contained, Google-Docs-ready HTML file per topic guide
#      (docs/export/*.html) with inlined SVGs and working cross-links.
#
# Requirements: graphviz (`dot`) and python3 (standard library only).
#
# Usage:  scripts/build_docs_export.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUIDES="$REPO_ROOT/docs/guides"
DIAGRAMS="$REPO_ROOT/docs/diagrams"
EXPORT="$REPO_ROOT/docs/export"
EXPORTER="$REPO_ROOT/scripts/export_to_google_docs.py"

echo "[*] Rendering diagrams (Graphviz .dot -> .svg)"
if command -v dot >/dev/null 2>&1; then
  for dot_src in "$DIAGRAMS"/src/*.dot; do
    [ -e "$dot_src" ] || continue
    name="$(basename "$dot_src" .dot)"
    dot -Tsvg "$dot_src" -o "$DIAGRAMS/$name.svg"
    echo "    rendered $name.svg"
  done
else
  echo "[!] 'dot' not found; skipping diagram render (using committed SVGs)"
fi

echo "[*] Exporting per-topic HTML"
mkdir -p "$EXPORT"

# One HTML file per topic guide (markdown is the canonical source).
for md in \
  01-architecture \
  02a-cluster-setup \
  02b-gpu-nodepool-dws \
  02c-deploy-inference \
  02d-deploy-jupyter \
  02e-verify-teardown \
  03-inference-endpoint-user-guide \
  04-jupyter-notebook-user-guide \
  05-remote-access-iap \
  appendix-glossary
do
  python3 "$EXPORTER" "$GUIDES/$md.md" -o "$EXPORT/$md.html"
done

echo "[*] Done. Exports in docs/export/"
