#!/bin/bash
# deploy/ops/grant-node-vm-ops.sh
#
# Grant VM-level ops (reset / stop / start / metadata / serial console) on the GPU
# nodes of a Flex-start pool, to the people listed in vm-ops-team.txt.
#
#   ./grant-node-vm-ops.sh                 # default: the TCPXO pool
#   CLUSTER=... ZONE=... POOL=... ./grant-node-vm-ops.sh
#
# This is the tier ABOVE grant-node-ssh.sh. SSH lets you use a node; this lets you
# reboot or shut one down. Run grant-node-ssh.sh as well -- neither implies the other.
#
# WHAT IT APPLIES
# ---------------
#   custom role gpuNodeVmOps      on each instance   -- the ops verbs (13 permissions)
#   custom role vmOpsPollMinimal  at PROJECT level   -- compute.zoneOperations.get/list
#
# Both roles are created by ./roles/apply-vm-ops-roles.sh; this script only binds them.
#
# WHY THERE ARE TWO ROLES AND NOT ONE
# -----------------------------------
# `gcloud compute instances stop|start|reset` issues the call and then POLLS the zone
# operation until it finishes. compute.zoneOperations.get is checked on the ZONE, not
# on the instance, and IAM flows downward only -- so an instance-level binding can
# never satisfy it. Bundling it into the instance role would look right, apply
# cleanly, and still fail at the poll with
#     Required 'compute.zoneOperations.get' permission
# *after the node has already been stopped* -- the worst possible place to fail. Hence
# a separate, read-only, project-level role. Exactly the same trap as
# sshResolveMinimal / compute.projects.get in grant-node-ssh.sh.
#
# WHY compute.instances.delete IS NOT IN THE ROLE
# -----------------------------------------------
# These are GKE-managed nodes: you remove one by resizing the node pool, never by
# deleting the VM. A hand-deleted node leaves the pool's intent untouched, so the
# autoscaler simply tries to re-grab -- and on Flex-Start A3 Mega capacity it may not
# come back. Ops does not need delete. If someone genuinely does, widen the role
# deliberately rather than reaching for roles/compute.instanceAdmin.v1, which also
# hands over disks, networking and instance creation.
#
# READ THIS BEFORE USING stop
# ---------------------------
# reset is a hard reboot: the VM keeps its DWS lease and its GPUs, so it is the safe
# verb for a wedged node and the reason this role exists.
#
# stop and suspend are NOT safe here. A Flex-Start (DWS) node that stops forfeits its
# lease, and re-acquiring 8x H100 Mega in asia-southeast1-c has taken days. That
# breaks the standing "the GPU is always held" rule, and GKE will meanwhile see the
# node NotReady and may recycle it. suspend additionally is not even supported on A3
# (GPUs + local SSD), so it will fail -- the permission is present for completeness,
# not because it works.
#
# WHY THIS SCRIPT HAS TO EXIST
# ----------------------------
# The instance binding is *instance-level* IAM, and Flex-start GPU nodes are ephemeral
# -- replaced on the 7-day retention boundary, on preemption, and on every pool
# recreate the capacity watchdog performs. A new node is a new IAM resource with an
# EMPTY policy, so ops access disappears each rotation with no error anywhere until
# someone tries to use it. The project-level role survives rotation; re-applying it is
# idempotent, so this script does both and is safe to run repeatedly.
#
# Run this after any node rotation, right next to grant-node-ssh.sh.
set -uo pipefail

PROJECT="${PROJECT:-hdlab-elideng}"
CLUSTER="${CLUSTER:-hypercomputer-a3-tcpxo}"
ZONE="${ZONE:-asia-southeast1-c}"
POOL="${POOL:-a3-mega-tcpxo-flex-pool}"

INSTANCE_ROLE="projects/${PROJECT}/roles/gpuNodeVmOps"
PROJECT_ROLE="projects/${PROJECT}/roles/vmOpsPollMinimal"

# Read the roster from vm-ops-team.txt so this script and the file can never disagree.
# Unlike the IAP tunnel policy in grant-node-ssh.sh these bindings are additive, so an
# empty list cannot revoke anyone -- but it would silently do nothing and report
# success, which is just as bad after a rotation. So fail loudly.
TEAM_FILE="${TEAM_FILE:-$(dirname "$0")/vm-ops-team.txt}"
if [ ! -r "$TEAM_FILE" ]; then
  echo "FATAL: cannot read $TEAM_FILE -- refusing to run. Without the roster this" >&2
  echo "       script would grant nothing and still exit 0." >&2
  echo "       The roster is .gitignore'd (this repo is public), so a fresh checkout" >&2
  echo "       will not have one. Start from the template:" >&2
  echo "         cp $(dirname "$0")/vm-ops-team.txt.example $TEAM_FILE" >&2
  exit 1
fi
mapfile -t TEAM < <(sed 's/#.*//' "$TEAM_FILE" | tr -d '[:blank:]' | grep -E '^user:.+@.+')
if [ "${#TEAM[@]}" -eq 0 ]; then
  echo "FATAL: no members parsed from $TEAM_FILE." >&2
  exit 1
fi

# Both custom roles must already exist; a missing role makes every binding below fail
# with a confusing "role not found" per node. Check once, up front.
for role in "$INSTANCE_ROLE" "$PROJECT_ROLE"; do
  if ! gcloud iam roles describe "${role##*/}" --project="$PROJECT" >/dev/null 2>&1; then
    echo "FATAL: custom role $role does not exist. Create it first:" >&2
    echo "       ./roles/apply-vm-ops-roles.sh" >&2
    exit 1
  fi
done

# vmOpsPollMinimal is project-scoped: it survives rotation, but re-applying is
# idempotent and makes this script self-sufficient for onboarding a new member.
echo "Project-level ($PROJECT)"
for member in "${TEAM[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="$member" --role="$PROJECT_ROLE" \
    --condition=None --quiet >/dev/null 2>&1 \
    && echo "    vmOpsPollMinimal  $member" \
    || echo "    FAILED vmOpsPollMinimal $member -- stop/start will hang at the operation poll"
done

# Nodes currently in the pool, matched on the GKE-applied node-pool label rather than
# on the node name -- names carry a random suffix that changes every rotation. Empty is
# not an error worth failing on: a Flex pool legitimately sits at zero nodes between
# grabs.
mapfile -t NODES < <(gcloud compute instances list \
  --project="$PROJECT" --zones="$ZONE" \
  --filter="labels.goog-k8s-node-pool-name=${POOL}" \
  --format="value(name)" 2>/dev/null)

if [ "${#NODES[@]}" -eq 0 ]; then
  echo "No nodes found in pool $POOL ($ZONE). Nothing instance-scoped to grant."
  exit 0
fi

echo "Pool $POOL -> ${#NODES[@]} node(s)"
for node in "${NODES[@]}"; do
  echo "--- $node"
  for member in "${TEAM[@]}"; do
    gcloud compute instances add-iam-policy-binding "$node" \
      --project="$PROJECT" --zone="$ZONE" \
      --member="$member" --role="$INSTANCE_ROLE" \
      --quiet >/dev/null 2>&1 \
      && echo "    gpuNodeVmOps  $member" \
      || echo "    FAILED gpuNodeVmOps $member"
  done
done

cat <<NOTE

Granted. Verify as the GRANTEE, not as yourself -- roles/owner satisfies all of this,
so an owner's successful test proves nothing. The IAM Policy Troubleshooter answers for
another principal without needing their credentials:

  curl -sS -X POST -H "Authorization: Bearer \$(gcloud auth print-access-token)" \\
    -H "Content-Type: application/json" -H "x-goog-user-project: ${PROJECT}" \\
    https://policytroubleshooter.googleapis.com/v1/iam:troubleshoot \\
    -d '{"accessTuple":{"principal":"<email>",
         "fullResourceName":"//compute.googleapis.com/projects/${PROJECT}/zones/${ZONE}/instances/${NODES[0]}",
         "permission":"compute.instances.reset"}}'

The x-goog-user-project header is required; without it the call fails in a way that
looks like a malformed response rather than an auth error. Note that a withheld
permission reads back as UNKNOWN_INFO_DENIED, not NOT_GRANTED, because the
troubleshooter cannot see org-level policy -- so it can confirm what IS granted but
cannot prove a negative. To check that delete really is withheld, inspect the roles the
principal holds instead.

VM ops does NOT imply SSH. Run ./grant-node-ssh.sh too if they need a shell.

Ops commands, once granted:
  gcloud compute instances reset <node> --zone ${ZONE} --project ${PROJECT}
  gcloud compute instances get-serial-port-output <node> --zone ${ZONE} --project ${PROJECT}
Prefer reset. See "READ THIS BEFORE USING stop" at the top of this script.
NOTE
