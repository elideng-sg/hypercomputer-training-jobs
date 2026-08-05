#!/bin/bash
# deploy/ops/grant-node-ssh.sh
#
# Grant the team IAP-tunnelled SSH onto the GPU nodes of a Flex-start pool.
#
#   ./grant-node-ssh.sh                 # default: the TCPXO pool
#   CLUSTER=... ZONE=... POOL=... ./grant-node-ssh.sh
#
# WHAT IT APPLIES (see docs/guides/05-remote-access-iap.md)
# ---------------------------------------------------------
#   enable-oslogin=TRUE               instance metadata    -- makes osAdminLogin effective
#   roles/compute.osAdminLogin        on the instance      -- login + sudo
#   roles/compute.viewer              on the instance      -- resolve the instance name
#   roles/iap.tunnelResourceAccessor  on the IAP *tunnel*  -- open the tunnel at all
#
# The other two are NOT instance-scoped and so survive rotation -- they are already in
# place for this team and this script leaves them alone:
#   custom role sshResolveMinimal (compute.projects.get)  at PROJECT level
#   roles/iam.serviceAccountUser  on the node's attached service account
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
PROJECT_NUMBER="${PROJECT_NUMBER:-151935633952}"   # IAP's REST API takes the NUMBER, not the id
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

# "user:a@x","user:b@x" -- the members array for the IAP tunnel policy below.
MEMBERS_JSON=$(printf '"%s",' "${TEAM[@]}"); MEMBERS_JSON="${MEMBERS_JSON%,}"

echo "Pool $POOL -> ${#NODES[@]} node(s)"
for node in "${NODES[@]}"; do
  echo "--- $node"

  # enable-oslogin=TRUE is what makes roles/compute.osAdminLogin actually do anything.
  # Without it gcloud falls back to writing an SSH key into instance/project metadata,
  # which needs compute.instances.setMetadata -- a permission guests do not (and should
  # not) have, so they fail with:
  #     Required 'compute.instances.setMetadata' permission for '...'
  # while the project OWNER sails through, because an owner can write metadata. That
  # asymmetry is why this went unnoticed: testing as the owner exercises the fallback
  # path, not the path the team uses.
  #
  # Set per-instance, deliberately:
  #   * NOT project-wide -- that changes SSH auth on every VM in the project, and this
  #     project has project-level `ssh-keys` metadata in use elsewhere.
  #   * NOT via node-pool metadata -- changing pool metadata recreates nodes, and these
  #     are scarce Flex-Start A3 Mega nodes that may not come back.
  gcloud compute instances add-metadata "$node" \
    --project="$PROJECT" --zone="$ZONE" \
    --metadata enable-oslogin=TRUE \
    --quiet >/dev/null 2>&1 \
    && echo "    enable-oslogin=TRUE" \
    || echo "    FAILED enable-oslogin -- guests will hit a setMetadata error"

  # roles/iap.tunnelResourceAccessor lives in a SEPARATE resource hierarchy from
  # Compute, so it cannot be granted with `gcloud compute instances
  # add-iam-policy-binding`, and a project-level grant of it is never consulted --
  # it silently does nothing. There is no gcloud surface for the per-instance tunnel
  # resource, hence curl. setIamPolicy REPLACES the policy, which is what we want:
  # every rotation starts from an empty policy and TEAM is the whole intended list.
  TUNNEL="https://iap.googleapis.com/v1/projects/${PROJECT_NUMBER}/iap_tunnel/zones/${ZONE}/instances/${node}"
  curl -sS -X POST \
      -H "Authorization: Bearer $(gcloud auth print-access-token --project="$PROJECT")" \
      -H "Content-Type: application/json" \
      "${TUNNEL}:setIamPolicy" \
      -d "{\"policy\":{\"bindings\":[{\"role\":\"roles/iap.tunnelResourceAccessor\",\"members\":[${MEMBERS_JSON}]}]}}" \
      >/dev/null 2>&1 \
    && echo "    iap.tunnelResourceAccessor  (all ${#TEAM[@]} members)" \
    || echo "    FAILED iap.tunnelResourceAccessor -- team CANNOT open a tunnel to $node"

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

cat <<NOTE

Granted. Verify the tunnel policy actually took -- an EMPTY policy reads back as
just {"etag":"ACAB"}, and note that checking PROJECT-level IAM for this role proves
nothing, because project-level grants of it are never consulted:

  curl -sS -X POST -H "Authorization: Bearer \$(gcloud auth print-access-token)" \\
    -H "Content-Type: application/json" \\
    "https://iap.googleapis.com/v1/projects/${PROJECT_NUMBER}/iap_tunnel/zones/${ZONE}/instances/${NODES[0]}:getIamPolicy" -d '{}'

Still needed, but NOT instance-scoped -- these survive rotation and are already set:

  * custom project role sshResolveMinimal (compute.projects.get). Must be at PROJECT
    level: IAM flows downward only, so an instance binding can never satisfy it.
  * roles/iam.serviceAccountUser on the node's attached service account.
  * The firewall rule allow-ssh-from-iap (35.235.240.0/20 -> tcp:22) must cover the
    nodes' network. It is on \`default\`, which the TCPXO pool's eth0 uses. Satisfied.

Then update the team-facing runbook Google Doc -- it names specific instances, so the
node names above make everyone's copy-pasted command fail with "resource not found".

Users connect with:
  gcloud compute ssh <node> --zone ${ZONE} --project ${PROJECT} --tunnel-through-iap
NOTE
