# 0001 — "Multi-zone" DWS requests were not actually zone-scoped

- **Severity:** High (no real multi-zone capacity hunt; risk of duplicate nodes in one zone)
- **Status:** Fixed
- **Date found:** 2026-07-16
- **Components:** GKE DWS ProvisioningRequest, `configs/dws_provisioning_request_zone_{a,b,c}.yaml`, `scripts/02_create_gke_cluster_dws.sh`
- **Fixed in:** PR #1

## Symptom
The setup claimed to spread DWS requests across `us-central1-a/b/c` to find H100
capacity faster, but in practice only one zone ever showed a resize request, and
the A3 node never appeared for 8+ hours. The "one request per zone" strategy did
not behave like one-per-zone.

## Root cause
The three per-zone request files differed **only** by a cosmetic pod label
`dws-zone: us-central1-<x>`. Their `nodeSelector` was identical:

```yaml
nodeSelector:
  node.kubernetes.io/instance-type: a3-highgpu-8g
  gpu-cluster: a3-h100
  # <-- no topology.kubernetes.io/zone
```

A metadata label does not constrain scheduling. With no `topology.kubernetes.io/zone`
selector, all three requests were interchangeable, so GKE could place them in the
same zone (or leave zones unused). Submitting all three also implicitly asks for
**3 nodes total** (each `podSet.count: 1`), not one.

Separately, `scripts/02_create_gke_cluster_dws.sh` documented `--location-policy=BALANCED`
in a comment and echo, but actually passed `--location-policy=ANY`.

## How it was diagnosed
```bash
# Only one zone had a resize request despite three "zone" configs:
for z in a b c; do
  mig=$(gcloud compute instance-groups managed list --project=hdlab-elideng \
        --filter="zone:(us-central1-$z) AND name~dws" --format="value(name)" | head -1)
  gcloud beta compute instance-groups managed resize-requests list "$mig" \
    --zone="us-central1-$z" --project=hdlab-elideng
done
# Inspecting the YAMLs showed identical nodeSelectors + only a differing label.
```

## Fix
Added a hard zone selector to each request's pod template:

```yaml
nodeSelector:
  node.kubernetes.io/instance-type: a3-highgpu-8g
  gpu-cluster: a3-h100
  topology.kubernetes.io/zone: us-central1-a   # b / c in the others
```

Also fixed the script's `location-policy` comment/echo to match the actual `ANY`
flag, added a warning that applying all three requests can provision up to
**3× a3-highgpu-8g (24× H100)**, and added `ONLY_ZONE=<a|b|c>` to submit a single node.

## Verification
After re-submitting, a resize request appeared in **each** zone independently:

```
us-central1-a  gke-default-a3-h100-req-zone-a-...  ACCEPTED
us-central1-b  gke-default-a3-h100-req-zone-b-...  ACCEPTED
us-central1-c  gke-default-a3-h100-req-zone-c-...  ACCEPTED
```

## Prevention / lessons
- **Labels ≠ scheduling constraints.** To pin a zone you need
  `topology.kubernetes.io/zone` in `nodeSelector`, not a descriptive label.
- Per-zone requests are independent and each asks for its own node — decide
  up front whether you want 1 node total or 1-per-zone, and cancel extras if
  you only need one.
- Keep script comments/echoes in sync with the flags actually passed.

## References
- Fix PR: #1
- Related: [0002](0002-dws-a3-node-reclaimed-after-10min.md) (keeping the provisioned node alive)
