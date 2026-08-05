#!/bin/bash
# deploy/ops/grant-node-ssh.sh
#
# Grant the team IAP-tunnelled SSH onto the GPU nodes of a Flex-start pool.
#
#   ./grant-node-ssh.sh                 # default: the TCPXO pool
#   CLUSTER=... ZONE=... POOL=... ./grant-node-ssh.sh
#
# WHY THIS SCRIPT HAS TO EXIST
# ----------------------------
# The access it grants is *instance-level* IAM, and Flex-start GPU nodes are
# ephemeral -- they are replaced on the 7-day retention boundary, on preemption, and
# on every pool recreate the capacity watchdog performs. A new node is a new IAM
# resource with an EMPTY policy, so the team silently loses SSH each rotation with
# no error anywhere until someone tries to connect.
#
# Run this after any node rotation. `deploy/ops/rearm-holder.sh` and the capacity
# watchdog both change nodes, so this belongs immediately after either one.
#
# WHY NOT JUST GRANT AT PROJECT LEVEL AND FORGET IT
# -------------------------------------------------
# roles/compute.osAdminLogin at project level would survive rotation, but it grants
# root-equivalent login on EVERY VM in the project -- including ubuntu-secure-desktop
# and every future node pool. Instance-level + a re-arm script keeps the blast radius
# at "the GPU nodes the team is meant to be using". That is a deliberate trade of
# convenience for scope; if you would rather have durability, the project-level
# grant is one command and this script becomes unnecessary.
set -uo pipefail

PROJECT="${PROJECT:-hdlab-elideng}"
CLUSTER="${CLUSTER:-hypercomputer-a3-tcpxo}"
ZONE="${ZONE:-asia-southeast1-c}"
POOL="${POOL:-a3-mega-tcpxo-flex-pool}"

# The team. osAdminLogin = login + sudo (matches what was granted on the old
# mega-flex node). compute.viewer is what lets `gcloud compute ssh` resolve the
# instance name; without it users must pass --zone and still hit a lookup error.
TEAM=(
  "user:elideng@google.com"
  "user:alexyin@google.com"
  "user:samaujs@google.com"
  "user:vivianzhangwei@google.com"
)

# Nodes currently in the pool, matched on the GKE-applied node-pool label rather
# than on the node name -- names carry a random suffix that changes every rotation.
# Empty is not an error worth failing on: a Flex pool legitimately sits at zero
# nodes between grabs.
mapfile -t NODES < <(gcloud compute instances list \
  --project="$PROJECT" --zones="$ZONE" \
  --filter="labels.goog-k8s-node-pool-name=${POOL}" \
  --format="value(name)" 2>/dev/null)

if [ "${#NODES[@]}" -eq 0 ]; then
  echo "No nodes found in pool $POOL ($ZONE). Nothing to grant."
  exit 0
fi

echo "Pool $POOL -> ${#NODES[@]} node(s)"
for node in "${NODES[@]}"; do
  echo "--- $node"
  for member in "${TEAM[@]}"; do
    gcloud compute instances add-iam-policy-binding "$node" \
      --project="$PROJECT" --zone="$ZONE" \
      --member="$member" --role="roles/compute.osAdminLogin" \
      --quiet >/dev/null 2>&1 \
      && echo "    osAdminLogin  $member" \
      || echo "    FAILED osAdminLogin $member"

    # The owner already sees every instance; granting viewer again is harmless but
    # noisy, so skip it. This mirrors the old mega-flex node, where the three
    # non-owner teammates held compute.viewer and elideng@ did not.
    if [ "$member" != "user:elideng@google.com" ]; then
      gcloud compute instances add-iam-policy-binding "$node" \
        --project="$PROJECT" --zone="$ZONE" \
        --member="$member" --role="roles/compute.viewer" \
        --quiet >/dev/null 2>&1 \
        && echo "    compute.viewer $member" \
        || echo "    FAILED compute.viewer $member"
    fi
  done
done

cat <<'NOTE'

Granted. Two things the team also needs, which are NOT instance-level:

  1. roles/iap.tunnelResourceAccessor -- required to open the IAP tunnel at all.
     Check with:
       gcloud projects get-iam-policy hdlab-elideng \
         --flatten='bindings[].members' \
         --filter='bindings.role=roles/iap.tunnelResourceAccessor' \
         --format='value(bindings.members)'
     If empty, no amount of instance-level osAdminLogin will let them connect.

  2. The firewall rule allow-ssh-from-iap (35.235.240.0/20 -> tcp:22) must cover
     the nodes' network. It is currently on `default`, which the TCPXO pool's eth0
     uses, so this is satisfied.

Users connect with:
  gcloud compute ssh <node> --zone asia-southeast1-c --tunnel-through-iap
NOTE
