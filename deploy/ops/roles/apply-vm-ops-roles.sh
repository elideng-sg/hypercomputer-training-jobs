#!/bin/bash
# deploy/ops/roles/apply-vm-ops-roles.sh
#
# Create-or-update the two custom roles that grant-node-vm-ops.sh binds. Idempotent:
# `create` on an existing role is not an error here, we fall through to `update`.
#
# Roles are project-level objects, so unlike the instance bindings they survive node
# rotation and you normally run this once. Re-run it after editing either YAML.
#
# Two gcloud quirks worth knowing, both of which look like bugs the first time:
#   * `description` is capped at 300 characters and the error only appears after the
#     confirmation prompt, so a too-long description reads as "the create failed".
#   * compute.instances.getGuestAttributes is in TESTING stage, so gcloud prompts for
#     confirmation. --quiet accepts it. That permission is a diagnostic nicety; drop it
#     from gpuNodeVmOps.yaml if you would rather not depend on a TESTING permission.
set -uo pipefail

PROJECT="${PROJECT:-hdlab-elideng}"
HERE="$(dirname "$0")"

apply() {
  local id="$1" file="$2"
  if gcloud iam roles describe "$id" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud iam roles update "$id" --project="$PROJECT" --file="$file" --quiet >/dev/null \
      && echo "  updated $id" || { echo "  FAILED update $id" >&2; return 1; }
  else
    gcloud iam roles create "$id" --project="$PROJECT" --file="$file" --quiet >/dev/null \
      && echo "  created $id" || { echo "  FAILED create $id" >&2; return 1; }
  fi
}

apply gpuNodeVmOps     "$HERE/gpuNodeVmOps.yaml"
apply vmOpsPollMinimal "$HERE/vmOpsPollMinimal.yaml"

echo
echo "Now bind them:  ../grant-node-vm-ops.sh"
