#!/usr/bin/env bash
#
# One-time IAM setup for per-user GCS workspaces on the TCPXO JupyterHub.
#
#   deploy/jupyter-gcs/setup-iam.sh            # apply
#   DRY_RUN=1 deploy/jupyter-gcs/setup-iam.sh  # print what would change
#
# Idempotent: safe to re-run. Creates two custom roles and grants them to the
# hub's Workload Identity principal.
#
# WHY TWO ROLES AND NOT ONE
#
# The hub creates a bucket per user. `storage.buckets.create` is evaluated
# against the *project*, not against a bucket, so an IAM condition on
# `resource.name` can never match it -- a conditioned create binding simply
# never grants anything. The dangerous permissions (reading and writing bucket
# IAM policies) *are* evaluated per bucket, so those go in a second role bound
# with a condition pinning them to the `hdlab-elideng-jupyter-` prefix.
#
# Net effect: the hub can create buckets anywhere in the project (a cost/noise
# risk, not a confidentiality one) but can only touch the IAM of buckets whose
# name starts with our prefix. It cannot read, write or grant itself access to
# unrelated buckets such as `hdlab-elideng-userdata`.
#
# KNOWN RESIDUAL RISK -- read this before deploying.
# Because the hub holds `storage.buckets.setIamPolicy` on prefix-matching
# buckets, a compromised hub could grant itself object access to a user's
# workspace. This is inherent to auto-provisioning: whatever creates per-user
# IAM can also subvert it. It is bounded to the workspace prefix, it is fully
# recorded in Cloud Audit Logs (SetIamPolicy is an admin activity log, on by
# default), and it is a strictly smaller blast radius than the alternative of a
# project-level storage admin. If that trade is unacceptable, the answer is
# admin-provisioned workspaces, not a tighter role.
set -euo pipefail

PROJECT="${PROJECT:-hdlab-elideng}"
PROJECT_NUMBER="${PROJECT_NUMBER:-151935633952}"
NAMESPACE="${NAMESPACE:-jupyter}"
HUB_KSA="${HUB_KSA:-hub}"
WORKLOAD_POOL="${WORKLOAD_POOL:-${PROJECT}.svc.id.goog}"
BUCKET_PREFIX="${BUCKET_PREFIX:-hdlab-elideng-jupyter-}"
LOCATION="${LOCATION:-asia-southeast1}"
SHARED_BUCKET="${SHARED_BUCKET:-${BUCKET_PREFIX}shared}"
DRY_RUN="${DRY_RUN:-}"

# Direct Workload Identity federation: the hub authenticates as its KSA. No
# service account, so there is no key to leak and nothing to rotate.
HUB_PRINCIPAL="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/subject/ns/${NAMESPACE}/sa/${HUB_KSA}"

ROLE_CREATE="jupyterWorkspaceBucketCreate"
ROLE_IAM="jupyterWorkspaceBucketIam"
CONDITION_EXPR="resource.name.startsWith(\"projects/_/buckets/${BUCKET_PREFIX}\")"

run() {
  if [[ -n "$DRY_RUN" ]]; then
    printf 'DRY_RUN: %q ' "$@"; echo
  else
    "$@"
  fi
}

# gcloud writes progress to stderr and exits non-zero on real failure; never pipe
# these through head/tail, which would mask the exit code.
role_exists() {
  gcloud iam roles describe "$1" --project="$PROJECT" >/dev/null 2>&1
}

ensure_role() {
  local role="$1" title="$2" description="$3" permissions="$4"
  if role_exists "$role"; then
    echo "role ${role} exists -- updating permissions to match"
    run gcloud iam roles update "$role" --project="$PROJECT" \
      --title="$title" --description="$description" \
      --permissions="$permissions" --stage=GA --quiet
  else
    echo "creating role ${role}"
    run gcloud iam roles create "$role" --project="$PROJECT" \
      --title="$title" --description="$description" \
      --permissions="$permissions" --stage=GA --quiet
  fi
}

echo "== project ${PROJECT} (${PROJECT_NUMBER})"
echo "== hub principal ${HUB_PRINCIPAL}"
echo

ensure_role "$ROLE_CREATE" \
  "JupyterHub workspace bucket create" \
  "Create per-user JupyterHub workspace buckets. Cannot be conditioned: buckets.create is checked against the project." \
  "storage.buckets.create"

ensure_role "$ROLE_IAM" \
  "JupyterHub workspace bucket IAM" \
  "Read and set IAM on JupyterHub workspace buckets. Granted only on the workspace name prefix. Grants no object access." \
  "storage.buckets.get,storage.buckets.getIamPolicy,storage.buckets.setIamPolicy"

echo
echo "== binding ${ROLE_CREATE} at project scope (unconditioned -- see header)"
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="$HUB_PRINCIPAL" \
  --role="projects/${PROJECT}/roles/${ROLE_CREATE}" \
  --condition=None \
  --quiet >/dev/null

echo "== binding ${ROLE_IAM} conditioned on the ${BUCKET_PREFIX} prefix"
run gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="$HUB_PRINCIPAL" \
  --role="projects/${PROJECT}/roles/${ROLE_IAM}" \
  --condition="expression=${CONDITION_EXPR},title=jupyter-workspace-buckets-only,description=Only buckets named ${BUCKET_PREFIX}*" \
  --quiet >/dev/null

echo
echo "== shared read-only dataset bucket ${SHARED_BUCKET}"
# Exists so a large public dataset is downloaded once, not once per user.
if gcloud storage buckets describe "gs://${SHARED_BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  echo "bucket gs://${SHARED_BUCKET} already exists"
else
  run gcloud storage buckets create "gs://${SHARED_BUCKET}" \
    --project="$PROJECT" --location="$LOCATION" \
    --uniform-bucket-level-access --public-access-prevention
fi

# Granted to every KSA in the namespace at once rather than per user, so the hook
# never has to touch the shared bucket's policy and the policy does not grow with
# the user count. This does include the namespace's `default` KSA and the hub --
# acceptable, because nothing private lives here and the grant is read-only.
# Writing to it is a deliberate admin action using project-level credentials.
NS_PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WORKLOAD_POOL}/namespace/${NAMESPACE}"
run gcloud storage buckets add-iam-policy-binding "gs://${SHARED_BUCKET}" \
  --project="$PROJECT" \
  --member="$NS_PRINCIPAL_SET" \
  --role="roles/storage.objectViewer" >/dev/null

echo
echo "== hub Kubernetes RBAC (needs serviceaccounts get/create)"
run kubectl apply -f "$(dirname "$0")/hub-rbac-extra.yaml"

echo
echo "Done. Verify with:"
cat <<EOF
  gcloud projects get-iam-policy ${PROJECT} \\
    --flatten='bindings[].members' \\
    --filter="bindings.members:${HUB_KSA}" \\
    --format='table(bindings.role, bindings.condition.expression)'
EOF
echo
echo "The hub holds NO object permissions. Confirm that stays true: neither role"
echo "above may ever list storage.objects.* -- that is the line this design draws."
