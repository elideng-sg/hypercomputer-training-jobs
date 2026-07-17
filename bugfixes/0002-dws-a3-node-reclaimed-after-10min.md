# 0002 — Provisioned A3 DWS node reclaimed ~10 minutes after boot

- **Severity:** Critical (you lose the scarce H100 node you waited hours for)
- **Status:** Fixed
- **Date found:** 2026-07-15
- **Components:** GKE DWS Queued Provisioning, ProvisioningRequest, capacity holder, `a3-h100-dws-pool`
- **Fixed in:** PR #2

## Symptom
A DWS `ProvisioningRequest` finally got capacity and an `a3-highgpu-8g` (8× H100)
node booted and joined the cluster — then, **~15 minutes later, the node was
deleted by GKE** and the cluster went back to 0 A3 nodes. No workload ever ran on it.

Observed timeline in `us-central1-a` (MIG `...e6c90733`):

```
07:03:37 PT  insert instance ...e6c90733-7jh2      (node created)
07:06 / 07:09 re-inserts of same instance          (MIG recreating during bootstrap)
07:09:45 PT  instance ready
07:24:12 PT  compute.instanceGroupManagers.deleteInstances   (node removed)
14:24:32 UTC "Stopped kubelet.service" + volume unmounts      (graceful shutdown = autoscaler, not a crash)
```

## Root cause
**Nothing in the cluster *consumed* the ProvisioningRequest.**

GKE DWS holds a freshly provisioned node for only a **~10-minute booking window**
after the request reaches `Provisioned=True`. A pod must be scheduled onto that
node within the window or the cluster autoscaler removes the node as "not needed."

A pod only counts as consuming the request if it carries **both** annotations
(note the `autoscaling.x-k8s.io/` prefix — *not* `cluster-autoscaler.kubernetes.io/`):

```yaml
autoscaling.x-k8s.io/consume-provisioning-request: <request-name>
autoscaling.x-k8s.io/provisioning-class-name: "queued-provisioning.gke.io"
```

The existing "capacity holders" did **not** have these:
- `configs/a3_dws_holder_8gpu.yaml` (Deployment) set only
  `cluster-autoscaler.kubernetes.io/safe-to-evict: false`.
- `configs/a3_dws_holder.yaml` (DaemonSet) set only `safe-to-evict` **and requested 0 GPUs**.

So the holders were never bound to the request, stayed `Pending/Unschedulable`,
never landed on the node, the booking expired unused, and the node was reclaimed.
`safe-to-evict: false` is irrelevant to a pod that was never scheduled.

## How it was diagnosed
```bash
# 1. Compute-layer timeline: node created then deleted ~15 min later, gracefully
gcloud compute operations list --zones=us-central1-a --project=hdlab-elideng \
  --filter="targetLink~e6c90733" \
  --format="table(operationType,targetLink.scope(instances),status,insertTime,endTime)"

# 2. Node shut down cleanly (kubelet stopped, volumes unmounted) => autoscaler, not crash
gcloud logging read 'resource.type="gce_instance" AND textPayload=~"kubelet"' \
  --project=hdlab-elideng --freshness=2d

# 3. Live proof: holder + job stuck Unschedulable, holder pod template has NO
#    consume-provisioning-request annotation (only safe-to-evict).
#    (queried via GKE REST API: GET /apis/apps/v1/namespaces/default/deployments
#     and GET /api/v1/namespaces/default/pods)
#    autoscaler visibility log for the old holder showed:
#      no.scale.up.nap.pod.gpu.no.limit.defined
```

## Fix
Added `configs/a3_dws_consumer_holders.yaml`: one **zone-pinned consumer holder
per request** (`a3-h100-req-zone-{a,b,c}`), each carrying the two
`autoscaling.x-k8s.io/*` annotations plus an explicit `safe-to-evict: false`,
requesting the full 8-GPU shape (`registry.k8s.io/pause:3.9`).

Because the holder is deployed **in parallel with the request** and is already
`Pending`-and-linked, the scheduler binds it to the node the instant it
provisions — inside the 10-minute window — which consumes the booking. Once it's
running, the node is occupied and `safe-to-evict: false` blocks later idle
scale-down for the 7-day flex-start window.

`scripts/02_create_gke_cluster_dws.sh` was reworked to submit requests first,
then deploy the consumers, and to drop the broken holder. The two old holder
manifests are marked DEPRECATED.

Two mechanisms, both required:
1. **consume annotation** → pod placed on the node within the booking window (defeats the initial reclaim).
2. **occupying pod + safe-to-evict:false** → node stays up (defeats later idle scale-down).

## Verification
After applying, the holder pods report (autoscaler event) — the healthy state:

```
IgnoredInScaleUp — Unschedulable pod ignored in scale-up loop, because it's
consuming ProvisioningRequest default/a3-h100-req-zone-a that is in Accepted state.
```

Before the fix, the broken holder instead produced
`no.scale.up.nap.pod.gpu.no.limit.defined`. The change from a NAP scale-up
attempt to "recognized request consumer" is the signal the binding is correct.

## Prevention / lessons
- **A holder must consume the request, not just be un-evictable.** `safe-to-evict:
  false` on an unscheduled pod does nothing.
- **The annotation prefix is `autoscaling.x-k8s.io/`.** `cluster-autoscaler.kubernetes.io/`
  is a different, older namespace and will silently fail to bind. Verify against
  the current GKE ProvisioningRequest docs before trusting an annotation key.
- The cleanest pattern is to make your **actual training Job** the consumer (same
  annotations) so real work occupies the node on arrival — a pause holder just
  parks scarce hardware (you pay for idle GPUs) until your workload is ready.
- The consumer's pod count / resource shape should match the request's `podSet`.

## References
- GKE: Deploy GPUs for batch workloads with ProvisioningRequest / DWS —
  https://cloud.google.com/kubernetes-engine/docs/how-to/provisioningrequest
- Fix PR: #2
- Related: [0001](0001-dws-zone-requests-not-zone-pinned.md) (the requests these consumers reference)
