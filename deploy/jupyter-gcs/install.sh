#!/usr/bin/env bash
#
# Deploy JupyterHub with per-user GCS workspaces.
#
#   deploy/jupyter-gcs/install.sh              # apply
#   DRY_RUN=1 deploy/jupyter-gcs/install.sh    # render and diff only
#
# Run deploy/jupyter-gcs/setup-iam.sh FIRST. This script sets
# blockWithIptables=false, which re-exposes the metadata server to user pods;
# that is only safe once the per-user KSAs and their bucket-scoped grants exist.
# The preflight below refuses to run if the IAM setup is missing.
set -euo pipefail

RELEASE="${RELEASE:-jhub}"
NAMESPACE="${NAMESPACE:-jupyter}"
CHART_VERSION="${CHART_VERSION:-4.4.0}"
PROJECT="${PROJECT:-hdlab-elideng}"
CLUSTER="${CLUSTER:-hypercomputer-a3-tcpxo}"
ZONE="${ZONE:-asia-southeast1-c}"
DRY_RUN="${DRY_RUN:-}"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_VALUES="${REPO_ROOT}/deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml"
GCS_VALUES="${REPO_ROOT}/deploy/jupyter-gcs/values-gcs.yaml"
MODULE="${REPO_ROOT}/deploy/jupyter-gcs/gcs_workspaces.py"

for f in "$BASE_VALUES" "$GCS_VALUES" "$MODULE"; do
  [[ -f "$f" ]] || { echo "missing $f" >&2; exit 1; }
done

echo "== preflight"

# 1. The unit tests are the only thing standing between a typo in the hook and a
# hub that refuses every spawn. They need no cluster, so there is no excuse.
( cd "${REPO_ROOT}/deploy/jupyter-gcs" && python3 -m pytest -q )

# 2. GCS FUSE CSI driver. Without it the pod stays Pending on an unmountable
# volume and the error surfaces only in kubectl describe.
if ! kubectl get csidriver gcsfuse.csi.storage.gke.io >/dev/null 2>&1; then
  cat >&2 <<EOF
GCS FUSE CSI driver is not installed on this cluster. Enable it first:

  gcloud container clusters update ${CLUSTER} --zone ${ZONE} \\
    --project ${PROJECT} --update-addons GcsFuseCsiDriver=ENABLED

If that fails with CLUSTER_ALREADY_HAS_OPERATION, a node upgrade is wedged --
check 'gcloud container operations list --filter=status!=DONE' before retrying.
EOF
  exit 1
fi
echo "gcsfuse CSI driver present"

# 3. The hub's IAM. Deploying the metadata-unblock without these bindings would
# leave user pods able to reach the metadata server while provisioning fails.
if ! gcloud iam roles describe jupyterWorkspaceBucketCreate \
     --project="$PROJECT" >/dev/null 2>&1; then
  echo "custom role jupyterWorkspaceBucketCreate is missing -- run deploy/jupyter-gcs/setup-iam.sh first" >&2
  exit 1
fi
if ! kubectl -n "$NAMESPACE" get role hub-gcs-workspaces >/dev/null 2>&1; then
  echo "Role hub-gcs-workspaces is missing -- run deploy/jupyter-gcs/setup-iam.sh first" >&2
  exit 1
fi
echo "hub IAM and RBAC in place"

# 4. The OAuth client secret is not in the repo (public repo). Read it back out
# of the running release, where it is stored inside the values.yaml key of
# secret/hub rather than as a flat key.
# awk must NOT `exit` on the first match here. Under `set -o pipefail` an early
# exit closes the pipe, `base64 -d` dies of SIGPIPE, the pipeline reports 141 and
# `set -e` kills this script -- silently, right after the last "ok" line. Match
# into a variable and print once at END instead.
SECRET="$(kubectl get secret hub -n "$NAMESPACE" -o jsonpath='{.data.values\.yaml}' \
  | base64 -d | awk '/client_secret:/ && !seen {v=$2; seen=1} END{print v}' | tr -d '"')"
if [[ -z "$SECRET" ]]; then
  cat >&2 <<'EOF'
Could not recover the Google OAuth client_secret from secret/hub.

Set it explicitly from the team password manager:
  SECRET=... deploy/jupyter-gcs/install.sh

Deploying with an empty secret leaves the hub *looking* healthy while every
Google sign-in fails with invalid_client.
EOF
  exit 1
fi
echo "recovered OAuth client secret (${#SECRET} chars)"

HELM_ARGS=(
  upgrade --install "$RELEASE" jupyterhub/jupyterhub
  --namespace "$NAMESPACE"
  --version "$CHART_VERSION"
  --values "$BASE_VALUES"
  --values "$GCS_VALUES"
  # One copy of the module in the repo; the chart turns it into a Secret and
  # mounts it next to z2jh.py. Omitting this makes templating fail rather than
  # deploying a hub without the hook.
  --set-file "hub.extraFiles.gcs_workspaces.stringData=${MODULE}"
  --set-string "hub.config.GoogleOAuthenticator.client_secret=${SECRET}"
  --timeout 10m
)

echo
if [[ -n "$DRY_RUN" ]]; then
  echo "== helm template (dry run)"
  helm "${HELM_ARGS[@]}" --dry-run >/dev/null
  echo "template rendered cleanly"
  exit 0
fi

echo "== helm upgrade"
helm "${HELM_ARGS[@]}"

echo
echo "== waiting for the hub to come back"
kubectl -n "$NAMESPACE" rollout status deploy/hub --timeout=5m

cat <<EOF

Deployed. Verify with a real login, not just a healthy pod:

  1. Sign in as yourself. The spawn now provisions a bucket, so first login for a
     new user takes noticeably longer than before.
  2. In a terminal:  ls -la ~/gcs && touch ~/gcs/hello && gcloud storage ls
  3. Confirm isolation -- as user A, this MUST fail:
       gcloud storage ls gs://hdlab-elideng-jupyter-<other-user>/
  4. Hub logs for the provisioning trail:
       kubectl -n ${NAMESPACE} logs deploy/hub | grep -i workspace
EOF
