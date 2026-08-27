# Per-user GCS workspaces for JupyterHub

Each JupyterHub user gets a **private GCS bucket**, mounted at `~/gcs` in their
notebook, provisioned automatically on first login. Datasets downloaded there
survive the pod, and no user can read another user's workspace.

Design rationale and the live evidence behind it:
[`docs/superpowers/specs/2026-08-26-jupyter-gcs-workspaces-design.md`](../../docs/superpowers/specs/2026-08-26-jupyter-gcs-workspaces-design.md).

## What a user sees

| Path | What it is | Persistence |
|---|---|---|
| `/home/jovyan` | 20Gi `premium-rwo` PVC — code, notebooks, git checkouts | survives pod restarts |
| `/home/jovyan/gcs` | their own GCS bucket, read/write | survives everything |
| `/home/jovyan/shared` | common datasets, read-only | admin-managed |

`gcloud storage`, `gsutil`, `gcsfs` and `google-cloud-storage` are all present, so
`pd.read_csv("gs://…")` works and bulk transfers can bypass the mount:

```bash
# in a notebook terminal
gcloud storage rsync -r gs://some-public-dataset ~/gcs/datasets/foo   # bulk: fast
hf download org/model --local-dir ~/gcs/models/thing                 # also fine
```

`hf`, not `huggingface-cli` — the image ships `huggingface_hub` 1.x, where the
old entry point is a stub that downloads nothing.

Use `gcloud storage` (not `cp` through the mount) for anything above a few GB —
gcsfuse is a filesystem shim, not a transfer tool.

## Files here

| File | Purpose |
|---|---|
| `gcs_workspaces.py` | the provisioning module; loaded into the hub as `pre_spawn_hook` |
| `test_gcs_workspaces.py` | unit tests — no cluster, no cloud |
| `test_profile_annotations.py` | guards the kubespawner brace trap (see below) |
| `values-gcs.yaml` | Helm overlay: mounts the module, unblocks metadata |
| `hub-rbac-extra.yaml` | the hub's `serviceaccounts: [get, create]` Role |
| `setup-iam.sh` | one-time: custom roles, hub bindings, shared bucket |
| `install.sh` | preflight + `helm upgrade` |
| `image/` | notebook images carrying the GCS toolchain |

## Deploying, in order

The order matters. Step 3 re-exposes the cloud metadata server to user pods;
doing that before step 2 would give every notebook a token for the shared
`default` ServiceAccount, which is exactly what the old iptables block existed to
prevent. `install.sh` refuses to run if step 2 is missing.

```bash
# 1. cluster addon (once)
gcloud container clusters update hypercomputer-a3-tcpxo \
  --zone asia-southeast1-c --project hdlab-elideng \
  --update-addons GcsFuseCsiDriver=ENABLED

# 2. IAM + RBAC + shared bucket (once, idempotent)
DRY_RUN=1 deploy/jupyter-gcs/setup-iam.sh   # read it first
deploy/jupyter-gcs/setup-iam.sh

# 3. images (once, and on every rebuild)
deploy/jupyter-gcs/image/build.sh

# 4. deploy
DRY_RUN=1 deploy/jupyter-gcs/install.sh
deploy/jupyter-gcs/install.sh
```

If step 1 fails with `CLUSTER_ALREADY_HAS_OPERATION`, a node upgrade is wedged —
see [`deploy/ops/node-rotation-runbook.md`](../ops/node-rotation-runbook.md).
Never pipe `gcloud` through `head`/`tail`: it masks the exit code, and that has
already hidden this exact failure once.

## How it works

On every spawn, `pre_spawn_hook` (idempotent, so the steady state is two reads):

1. Sanitizes the username → `<sanitized>`.
2. Ensures KSA `jupyter-user-<sanitized>` exists in `jupyter`, annotated with the
   original username.
3. Ensures bucket `hdlab-elideng-jupyter-<sanitized>` exists (UBLA, public access
   prevention enforced, `asia-southeast1`).
4. Ensures a bucket-level `roles/storage.objectUser` binding for that KSA's
   direct Workload Identity principal, then waits until the binding reads back.
5. Sets `spawner.service_account` and stashes the resolved names on the spawner.

Then `modify_pod_hook` attaches the gcsfuse CSI volumes, the mounts and the
`gke-gcsfuse/*` annotations to the finished pod manifest, and returns it.

**Two hooks, and the split is load-bearing — do not fold the mounts back into
`pre_spawn_hook`.** kubespawner applies a profile's `kubespawner_override` *after*
`pre_spawn_hook`, and its `_apply_overrides` merges an override into the existing
trait only when both sides are dicts; anything else is a plain `setattr`. A
profile that supplies `volumes` therefore *replaces* whatever the hook appended.
Tried live on 2026-08-27: provisioning logged success, the pod ran as the right
KSA, and it had no `~/gcs` at all. `modify_pod_hook` runs on the final manifest,
after every override, which is the only place this can be done safely.

Identity is **direct Workload Identity federation** — a `principal://` member per
KSA, no per-user GSA, so there is no service account key anywhere in this design.

**It fails closed.** If any step fails the spawn aborts with a message naming the
failure. Handing someone a notebook whose `~/gcs` is missing — or is someone
else's — is the one outcome worth failing a spawn to avoid.

**It never deprovisions.** Removing a user is a deliberate admin action.

### Why a bucket per user, not a folder per user

The original design used one bucket with a GCS managed folder per user. It was
tested live on 2026-08-26 and **does not work**: gcsfuse and the CSI driver both
check `storage.objects.list` at *bucket* scope, which managed-folder IAM does not
satisfy. The measured result was a workspace the user could read and write but
never `ls`, and a volume that failed to mount at all — `skipCSIBucketAccessCheck`
only moved the same denial into the sidecar. Granting bucket-level list to fix it
would have exposed every user's object names to every user.

A bucket per user costs nothing extra (GCS bills on stored bytes, not buckets)
and makes isolation a property of the resource boundary rather than of a
condition expression.

### Username sanitization

Lowercase; non-alphanumerics to `-`; trimmed to fit the 63-char bucket limit
after the `hdlab-elideng-jupyter-` prefix. Whenever normalization changes
anything — or the result is a reserved name like `shared` — a 5-hex-char hash of
the *original* username is appended, so `a.b` and `a-b` cannot land on the same
bucket. `elideng`, `samaujs` and `kzuo` pass through unchanged.

## Security posture

What the design guarantees:

- A user's KSA is granted `objectUser` on **exactly one** bucket: their own.
- No service account keys exist; identity is federated per KSA.
- The hub holds **no object permissions at all** — it cannot read or write a
  single object in any bucket.
- Buckets are created with uniform bucket-level access and public access
  prevention enforced, so an accidental ACL cannot make one public.

What it does **not** guarantee, stated plainly:

- **The hub can escalate within the workspace prefix.** Auto-provisioning
  requires `storage.buckets.setIamPolicy`, so a compromised hub could grant
  itself object access to a user's workspace bucket. The grant is conditioned on
  `resource.name.startsWith("projects/_/buckets/hdlab-elideng-jupyter-")`, so
  nothing outside the workspace prefix is reachable, and every `SetIamPolicy` is
  recorded in Cloud Audit Logs by default. This is inherent to self-service:
  whatever creates per-user IAM can also subvert it. The alternative is
  admin-provisioned workspaces.
- **`storage.buckets.create` cannot be name-scoped.** It is evaluated against the
  project, so a condition on `resource.name` would never match and the binding
  would grant nothing. The hub can therefore create buckets anywhere in the
  project — a cost and noise risk, not a confidentiality one.
- **No per-user storage quota.** GCS has no such thing. A user can fill a bucket
  and run up the bill. Monitor it; a lifecycle rule is the mitigation if it
  becomes a problem.

Two independent mechanisms had to be opened for any of this to work, and both are
in `values-gcs.yaml`: the iptables block on the metadata server
(`cloudMetadata.blockWithIptables`) **and** the singleuser NetworkPolicy's
metadata egress rule (`networkPolicy.egressAllowRules.cloudMetadataServer`). This
cluster runs Dataplane V2, so the NetworkPolicy is enforced. A hand-run debug pod
is not labeled `component: singleuser-server` and so escapes that policy — it
will reach GCS while real notebooks cannot. Verify with a real spawn.

## Testing

```bash
cd deploy/jupyter-gcs && python3 -m pytest -q
```

No cluster or cloud credentials needed; the GCS API and Kubernetes client are
faked. `install.sh` runs these as a preflight.

`test_profile_annotations.py` guards a defect that took production down before:
kubespawner runs every `extra_annotations` value through `str.format()`, so the
literal JSON braces in `networking.gke.io/interfaces` must be **doubled** in the
values file. Single braces fail every GPU spawn with
`KeyError: '"interfaceName"'`. Do not "tidy up" those braces.

### Live verification that actually proves something

```bash
# as user A, in a notebook terminal
ls -la ~/gcs && echo hi > ~/gcs/mine.txt && gcloud storage ls gs://hdlab-elideng-jupyter-<A>/

# the isolation test -- this MUST fail with AccessDenied
gcloud storage ls gs://hdlab-elideng-jupyter-<B>/
cat ~/shared/../gcs-b/anything 2>/dev/null || echo "correctly inaccessible"
```

A passing positive test proves plumbing. Only the failing negative test proves
isolation.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Spawn fails, "Could not prepare your GCS workspace" | `kubectl -n jupyter logs deploy/hub \| grep -i workspace` — the message names the step |
| Pod stuck Pending, `MountVolume.SetUp failed` | Is the CSI addon enabled? `kubectl get csidriver gcsfuse.csi.storage.gke.io` |
| `PermissionDenied … storage.objects.list` on mount | The bucket-level `objectUser` binding is missing or not yet propagated; check the bucket's IAM policy |
| Mount succeeds, but SDK/gcloud calls hang or time out | Metadata server unreachable — check **both** `blockWithIptables` and the NetworkPolicy egress rule |
| `~/gcs` empty but the bucket has objects | `implicit-dirs` missing from mount options, or you are looking at a different bucket |
| Spawn works, `gcloud` missing | Profile is still on a stock image; check `singleuser.image` and the profile overrides |
| Every GPU spawn dies with `KeyError` | The interfaces annotation braces got un-doubled |
| **Spawn succeeds but there is no `~/gcs` at all**, and the hub logged provisioning success | `modify_pod_hook` is not registered, or the mounts were moved back into `pre_spawn_hook` where a profile override wipes them. `kubectl -n jupyter get pod jupyter-<user> -o jsonpath='{.spec.volumes[*].name}'` — believe the manifest, not the log |
| **No `/home/jovyan` mount on a GPU profile** (files vanish when the pod is replaced) | That profile is overriding `volumes` as a *list*, which replaces the chart's dict and takes the home PVC with it. Use a map keyed by volume name; `pytest deploy/jupyter-gcs/` catches this |
| Pod stuck Pending, `transport endpoint is not connected`, sidecar already Running | An invalid gcsfuse `mountOption`. Check the CSI event for `unknown flag: --…`. Read-only belongs in `csi.readOnly`, not in `mountOptions` |
| Rebuilt an image but nothing changed | The tag was re-pushed. Notebook containers pull `IfNotPresent`, so nodes keep the cached layer — bump the tag in `build.sh` *and* in `03-jupyter-values-tcpxo.yaml` |
| `ModuleNotFoundError: pandas` in a notebook | Profile is on an image older than `v2`, or on a stock `minimal-notebook` base |
