# Per-User GCS Workspaces for JupyterHub on TCPXO — Design Spec

- **Date:** 2026-08-26
- **Status:** Implemented. **Revised mid-build:** the approved managed-folder
  isolation mechanism failed live verification (R1 below) and was replaced, with
  the owner's explicit agreement, by **one bucket per user**. §1–§3 describe the
  shipped design; the original managed-folder design is preserved in R1 so the
  reasoning is not lost. Code in [`deploy/jupyter-gcs/`](../../../deploy/jupyter-gcs/README.md).
- **Cluster:** `hypercomputer-a3-tcpxo`, zone `asia-southeast1-c`, project `hdlab-elideng` (project number `151935633952`)
- **Branch:** `feat/jupyter-gcs-workspaces`, based on `worktree-tcpxo-migration` (**not** `main` — the TCPXO JupyterHub config exists only on that branch)
- **Depends on:** live JupyterHub (z2jh chart 4.4.0, release `jhub`, ns `jupyter`, Google OAuth), Workload Identity pool `hdlab-elideng.svc.id.goog`

## Goal

Let JupyterHub users download their own datasets and keep them in Cloud Storage, use them from notebooks as ordinary files, and have an **isolated per-user workspace** in GCS for all their artifacts — isolated by IAM, not merely by convention.

Today a notebook has no cloud identity at all: user pods run as the shared `default` ServiceAccount and an init container iptables-DROPs the metadata server, so no GCS access of any kind is possible.

## Decisions (from brainstorming)

| Dimension | Decision |
|-----------|----------|
| Isolation | **One bucket per user**, bucket-level IAM. *(Revised — was one bucket + a managed folder per user; see R1.)* |
| Provisioning | **Hub auto-provisions on first login** (`pre_spawn_hook`), no admin step |
| Access path | **FUSE mount *and* CLI/SDK** — `~/gcs` for filesystem workflows, `gcloud storage`/`gcsfs` for bulk |
| Bucket | **Regional, `asia-southeast1`**, co-located with the cluster |
| Per-user identity | One KSA per user + **direct Workload Identity federation** (no GSA per user) |
| Hierarchical namespace | **Off** — no longer load-bearing once managed folders were dropped |
| Deprovisioning | **Out of scope.** Nothing is ever deleted automatically |

## 1. Storage layout

**One bucket per user**, plus one shared bucket:

```
gs://hdlab-elideng-jupyter-<sanitized>    private workspace, one per user
gs://hdlab-elideng-jupyter-shared         read-only to all users, admin-writable
```

Every bucket: region `asia-southeast1`, uniform bucket-level access, public access prevention enforced, HNS off, label `managed-by=jupyterhub-gcs-workspaces`.

Buckets cost nothing in themselves — GCS bills stored bytes, not buckets — and this makes isolation a property of the resource boundary rather than of a condition expression. The trade is a flat namespace: there is no single prefix listing to enumerate all workspaces, so `gcloud storage ls --project` plus the label is how an admin finds them.

"Admin-writable" for the shared bucket means a human with project-level `roles/storage.admin` populates it by hand. No automation writes there. It exists so a large public dataset is downloaded once rather than once per user, and it is granted read-only to the namespace's whole principal set (`principalSet://…/namespace/jupyter`) rather than per user — so the hook never touches its policy and its policy does not grow with the user count.

**Username sanitization.** The JupyterHub username becomes both a KSA name and a bucket suffix, so it is normalized: lowercase, non-conforming characters to `-`, collapse repeats, strip leading/trailing `-`, and truncate to fit. The bucket-name limit is 63 characters and the prefix `hdlab-elideng-jupyter-` consumes 22, so the sanitized name is capped at **41** characters (computed from the configured prefix, not hardcoded). A `-` plus the first 5 hex of the SHA-256 of the original name is appended **whenever normalization changed anything** (so `a.b` and `a-b` cannot collide) **or the result is a reserved name** — `shared`, `hub`, `default`, `admin`, `public`. Without that reservation a user called `shared` would have been granted `objectUser` on the shared dataset bucket. The mapping is recorded as a KSA annotation `lab.hdlab/jupyterhub-username` holding the original name. Current users (`elideng`, `samaujs`, `kzuo`) pass through unchanged.

## 2. Per-user identity

- KSA `jupyter-user-<sanitized>` in ns `jupyter`. KubeSpawner sets `service_account` per spawn.
- Direct WI federation — **no per-user GSA**, no key material:
  ```
  principal://iam.googleapis.com/projects/151935633952/locations/global/workloadIdentityPools/hdlab-elideng.svc.id.goog/subject/ns/jupyter/sa/jupyter-user-<sanitized>
  ```
- Grants for that principal:

| Resource | Role | Why |
|---|---|---|
| `gs://hdlab-elideng-jupyter-<u>` (their own bucket) | `roles/storage.objectUser` | read/write/delete **and list** their own workspace |
| `gs://hdlab-elideng-jupyter-shared` | `roles/storage.objectViewer`, granted to the namespace principal set | read common datasets |

Nothing else. A user's principal appears in exactly one workspace bucket's policy — their own — so cross-user access is not a matter of getting a condition right; the permission simply does not exist. Bucket-level `objectUser` is safe *because* the bucket is the unit of ownership, which is precisely what the managed-folder design could not achieve (R1).

## 3. Hub auto-provisioning (the privileged component)

Chosen for self-service, so the hub needs a GCP identity. **Direct Workload Identity federation for the hub KSA too** — no GSA at all, an improvement on the originally specced `jhub-provisioner@` GSA, since it removes a key-bearing identity from the design.

Per-user buckets force one uncomfortable consequence: the hub needs `storage.buckets.create`, and **that permission cannot be name-scoped** — it is evaluated against the *project*, so an IAM condition on `resource.name` never matches and such a binding grants nothing. The mitigation is to split the grant in two, so only the harmless half is unconditioned:

| Custom role | Permissions | Binding |
|---|---|---|
| `jupyterWorkspaceBucketCreate` | `storage.buckets.create` | project, **unconditioned** |
| `jupyterWorkspaceBucketIam` | `storage.buckets.get`, `.getIamPolicy`, `.setIamPolicy` | project, conditioned on `resource.name.startsWith("projects/_/buckets/hdlab-elideng-jupyter-")` |

- The hub holds **no object permissions at all** — it cannot read or write a single object in any bucket.
- It cannot touch the IAM of any bucket outside the workspace prefix, so it cannot grant itself access to e.g. `hdlab-elideng-userdata`.
- It can create buckets anywhere in the project. That is a cost and noise risk, not a confidentiality one, and it is unavoidable as described above.
- **Residual risk, stated plainly:** holding `setIamPolicy` on prefix-matching buckets means a compromised hub could grant itself object access to a user's workspace. This is inherent to auto-provisioning — whatever creates per-user IAM can also subvert it. Bounded to the prefix, recorded in Cloud Audit Logs (`SetIamPolicy` is admin-activity, on by default). The alternative is admin-provisioned workspaces. This weakens success criterion 4 and is called out there.
- Hub's k8s RBAC gains `serviceaccounts: [get, create]` via a separate Role (`deploy/jupyter-gcs/hub-rbac-extra.yaml`; the chart exposes no hook for extra rules, and being separate means `helm upgrade` will not clobber it). No `delete`, no `patch`, no `update` — the hub must not be able to repoint an existing user's identity.

`pre_spawn_hook` (wired via `hub.extraConfig`), idempotent, runs on every spawn — the steady state is two reads:

1. Sanitize the username.
2. Ensure KSA `jupyter-user-<sanitized>` exists (create if absent, annotated with the original username). A 409 is tolerated as a lost race with a concurrent spawn.
3. Ensure bucket `hdlab-elideng-jupyter-<sanitized>` exists (UBLA + PAP enforced at creation, so an accidental ACL cannot make it public). A 409 is tolerated.
4. Ensure the bucket-level `objectUser` binding for the user's principal exists — read-modify-write preserving the etag and every other binding, so an admin binding is never clobbered.
5. If a binding was just added, poll until it reads back, with bounded backoff. IAM is eventually consistent and the first mount would otherwise 403.
6. Set `spawner.service_account` and attach the CSI volumes, all as Python objects — never via string templating (see R4).

Implemented against `kubernetes_asyncio` + `google.auth`/`requests` REST calls, because the z2jh hub image ships those but has **neither** `google-cloud-storage` nor the synchronous `kubernetes` client. The blocking GCS half runs in an executor so a slow IAM poll does not stall the hub's event loop.

**Fails closed.** If any step fails the spawn aborts with an actionable message. Handing a user a notebook whose `~/gcs` silently is not theirs — or is someone else's — is the one outcome worth failing a spawn to avoid.

**Never deprovisions.** Removing a user is a deliberate admin action, documented in the runbook, not something an internet-facing hub can do.

## 4. Notebook side

- **GCS FUSE CSI driver** enabled on the cluster (done — see Verified findings).
- **`singleuser.cloudMetadata.blockWithIptables: false`** — mandatory. Nothing can authenticate to GCS while 169.254.169.254 is DROPped, and the block applies to the whole pod network namespace, so it disables the gcsfuse sidecar too.

  This is the one security-posture change in the design, and it is only acceptable *because* of §2: the identity a user can now reach through the metadata server is their own bucket-scoped KSA. Before this change the reachable identity would have been the shared `default` SA, which is exactly why the block was there. **The block must not be lifted before per-user KSAs are in place** — the ordering is a correctness requirement, not a preference, and `install.sh` preflights it.
- **`singleuser.networkPolicy.egressAllowRules.cloudMetadataServer: true`** — equally mandatory, and a **second, independent gate** (see R5). The chart's singleuser NetworkPolicy puts `169.254.169.254/32` in the `except` list of its allow-all egress rule and permits only DNS ports to it. This cluster runs GKE Dataplane V2 (`ADVANCED_DATAPATH`), so that policy is enforced: with only `blockWithIptables: false`, notebooks still cannot reach the metadata server on port 80 and every token fetch times out. Same ordering requirement as above.
- Mounts (CSI inline ephemeral volumes, `gcsfuse.csi.storage.gke.io`):

| Path | Contents | Options |
|---|---|---|
| `/home/jovyan/gcs` | their own bucket | `implicit-dirs`, uid/gid 1000 |
| `/home/jovyan/shared` | shared bucket | `implicit-dirs`, uid/gid 1000, `read_only`, `readOnly: true` |

No `only-dir` any more — the bucket *is* the workspace.

- **The 20Gi `premium-rwo` PVC stays as `/home/jovyan`.** Code, notebooks, git checkouts and in-progress checkpoints belong on a real disk; GCS holds data and finished artifacts. Putting the home directory itself on FUSE breaks Jupyter's checkpoint/atomic-rename behavior.
- Sidecar gets explicit resources via annotations (`gke-gcsfuse/cpu-limit`, `memory-limit`, `ephemeral-storage-limit`). The **ephemeral-storage limit is what makes multi-GB dataset downloads work** — gcsfuse buffers writes to local disk, and the default is too small for a 50GB download.
- **Two** images from one `Dockerfile` (`BASE_IMAGE` build arg), because the CPU profile should not pull a 10GB CUDA image to read a CSV:

| Image | Base | Used by |
|---|---|---|
| `notebook-gcs:v1` (~2GB) | `quay.io/jupyter/minimal-notebook` | `singleuser.image` → CPU profile |
| `pytorch-notebook-gcs:v1` (~10GB) | `quay.io/jupyter/pytorch-notebook:cuda12-latest` | both GPU profiles, `prePuller.extraImages` |

  Both add `gcloud`/`gsutil` + `gcsfs` + `google-cloud-storage` + `huggingface_hub` + `kaggle`, none of which the stock images have. The GPU base is unchanged from what the GPU profiles already ran, so CUDA and torch behaviour does not move. Build-time smoke test (`gcloud --version`, import check) so a broken image fails in Cloud Build rather than on a user's first spawn.
  A **new Artifact Registry repo in `asia-southeast1`** — the existing `lab-images` is in `asia-east1`, and this is a ~10GB CUDA image pulled on every new node. Repo names need only be unique per location, so it is also called `lab-images`.
- Example notebook `deploy/jupyter/examples/dataset_to_gcs.ipynb`: download (HF / `curl`) → land in `~/gcs` → read back both as files and via `gcsfs` → and explicitly **when to stop using FUSE** and switch to `gcloud storage rsync` for bulk.

## 5. Failure modes

| Failure | Handling |
|---|---|
| Provisioning error | Spawn fails closed with actionable message; hub logs the step that failed |
| IAM propagation lag | Hook verifies access with bounded retry before returning |
| Mount failure | Documented triage: sidecar logs → metadata blocking → folder IAM → bucket-meta role |
| User fills the bucket | **Accepted risk.** GCS has no per-user quota. Mitigated by monitoring + documented lifecycle rule, not enforced |
| Username collision after sanitization | Hash suffix (§1) |
| Node pool auto-upgrade re-wedges | Runbook entry; see Verified findings R3 |

## 6. Testing

Pure logic, unit-tested with pytest, no cluster and no cloud — `cd deploy/jupyter-gcs && pytest -q`, also run as an `install.sh` preflight. 58 tests:
- `test_gcs_workspaces.py` — username sanitizer (stability, collision resistance, length, reserved names, annotation round-trip); provisioner against faked GCS/k8s clients (creates-when-absent, no-op-when-present, etag/binding preservation, IAM convergence and give-up, 409 races tolerated, non-API exceptions not swallowed, fails-closed); spawner wiring (existing GPU profile volumes preserved, no braces in annotation values, two users get different buckets and neither appears in the other's policy).
- `test_profile_annotations.py` — carried over from `fix/jupyter-tcpxo-spawn` (it did not exist on this branch), converted to pytest and extended to assert the *rendered* annotation is the 9-interface JSON GKE expects, not merely that `format()` did not raise (see R4).

Live verification:
1. **Negative control — the isolation proof.** User A must get 403 listing *and* reading user B's folder, and 403 listing the bucket root. This is the test that decides whether the design is sound; everything else is plumbing.
2. Positive: write/read/list inside own folder, via both FUSE and SDK.
3. A real multi-GB download landing in `~/gcs`.
4. First-login provisioning for a never-before-seen user.
5. Existing users (`elideng`, `samaujs`, `kzuo`) still get their existing PVC home.
6. GPU spawn still works — the TCPXO 8-GPU profile must be unaffected (see R4).

## Verified findings (live, 2026-08-26)

- **Workload Identity enabled**, pool `hdlab-elideng.svc.id.goog`. Direct `principal://` bindings work with no GSA.
- **R2 resolved: managed folders work on a plain UBLA, non-HNS bucket.** Bucket and `users/spiketest/` created successfully. HNS stays off; the cost is slower gcsfuse rename/list on huge trees.
- **GCS FUSE CSI driver now ENABLED** — `gcsfuse.csi.storage.gke.io`, `gcsfusecsi-node` running 4/4 nodes.
- **R3 — node pool auto-upgrade was wedged and blocked the addon.** `UPGRADE_NODES` on `a3-mega-tcpxo-flex-pool` had been RUNNING since 2026-08-22T00:20 (~2.5 days), locking out all cluster config changes. Root cause: pool is autoscaling with `totalMaxNodeCount: 3` and already had 3 nodes, so a surge upgrade had nowhere to place a replacement, while all 3 nodes were packed with `gpu-holder-tcpxo` pods holding scarce DWS Flex-Start A3-mega capacity. Cancelled 2026-08-26 to unblock. **GPU nodes remain on `1.35.6-gke.1641000` while the pool target is `1.35.6-gke.1710000`.** `autoUpgrade: true` on channel REGULAR means **this will recur** — a maintenance exclusion, or a capacity plan that leaves surge room, is needed. Out of scope here; belongs in the node-rotation runbook.
- **Metadata blocking confirmed present:** init container `block-cloud-metadata` runs `iptables --append OUTPUT -p tcp -d 169.254.169.254 --dport 80 -j DROP`.
- **Notebook image gap confirmed:** no `gcloud`, no `gsutil`, no `google-cloud-storage`, no `gcsfs`. Internet egress from user pods works.
- `hub.extraConfig` is currently `{}` — no conflict with the new hook.
- Hub Role currently covers `pods, persistentvolumeclaims, secrets, services` only — `serviceaccounts` must be added.
- **R4 — pre-existing repo defect, in a file this work must edit.** The live hub runs the TCPXO interfaces annotation with **doubled** braces; `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` on the tip branch has **single** braces, which reproduces the production `KeyError: '"interfaceName"'` (kubespawner runs annotation values through `str.format()`). The fix and its test exist only on the stale side branch `fix/jupyter-tcpxo-spawn`, which is 7 commits behind. Two files describe the same config and the newer one is the broken one. **Fixed:** `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` now carries the doubled braces (verified against the rendered chart output, which matches live), with a comment explaining why they must stay doubled, and `test_profile_annotations.py` fails if anyone un-doubles them. It is also why the hook builds volumes and annotations as Python objects rather than by templating.

- **R1 (the crux) — FAILED. The managed-folder design does not work.** Tested live with KSA `jupyter-user-spiketest` holding `objectUser` on managed folder `users/spiketest/` plus `storage.buckets.get` on the bucket:
  - **CSI mount failed:** `MountVolume.SetUp failed … PermissionDenied … Caller does not have storage.objects.list access to the … bucket. Permission 'storage.objects.list' denied on resource '//storage.googleapis.com/projects/_/buckets/hdlab-elideng-jupyter-asiasoutheast1'`.
  - `skipCSIBucketAccessCheck: "true"` did **not** help — it relocated the identical denial into the sidecar ("sidecar bucket access check error").
  - **SDK probe:** writing and reading inside the own folder worked, `buckets.get` worked, **listing the own folder was DENIED**, and all four negative controls were correctly denied.
  - **Conclusion:** managed-folder IAM grants read/write but not listing, because `storage.objects.list` is evaluated at *bucket* scope regardless of prefix. Granting it bucket-wide to fix the mount would expose every user's object names to every user — which defeats the entire design. There is no isolation-preserving variant of the managed-folder approach.
  - **Resolution:** went back to the owner rather than substituting silently (as this spec committed to doing). Owner chose the documented fallback, **one bucket per user**. Re-verified live with probe `r1b-probe`: FUSE `ls`/write/read/nested-dir/20MB-file all worked, SDK listing of the own bucket worked, all four negative controls denied, zero `FailedMount` events. §1–§3 rewritten accordingly.
- **R5 — a second, independent metadata gate that the spike had masked.** `singleuser.cloudMetadata.blockWithIptables: false` alone is **not sufficient**. The chart's singleuser NetworkPolicy `except`-lists `169.254.169.254/32` and allows only DNS ports to it, and this cluster runs Dataplane V2 (`ADVANCED_DATAPATH`), so it is enforced — notebooks would still have failed every token fetch. Found by rendering the chart and reading the policy, *not* by the live probe: a hand-run probe pod is not labeled `component: singleuser-server`, so the policy never selected it and the probe reached GCS while real notebooks could not have. Fix: `singleuser.networkPolicy.egressAllowRules.cloudMetadataServer: true`. **Generalisable lesson: a spike pod that skips the labels the real workload carries can skip its policies too.**

## Verified findings (live end-to-end, 2026-08-27)

The first real spawn found the feature non-functional, in a way no unit test could have caught. Four defects, in the order they surfaced.

- **E1 — the mounts never reached the pod. `pre_spawn_hook` is the wrong hook for volumes.** The spawn *succeeded*: provisioning logged fine, the pod ran as `jupyter-user-elideng` — and had no CSI volumes, no `~/gcs`, no `~/shared`, no gcloud identity. Root cause is kubespawner's `_apply_overrides`:

  ```python
  if isinstance(v, dict) and isinstance(getattr(self, k), dict):
      recursive_update(getattr(self, k), v)   # dicts MERGE
  else:
      setattr(self, k, v)                     # everything else REPLACES
  ```

  A profile's `kubespawner_override` is applied **after** `pre_spawn_hook`, so anything the hook appended to `spawner.volumes` was thrown away by the TCPXO profile's list-form `volumes`. Compounding it: z2jh sets `c.KubeSpawner.volumes` to a **dict keyed by volume name**, so `list(spawner.volumes)` silently yields the *key strings* rather than failing. **Fix:** inject volumes/mounts/annotations from `modify_pod_hook`, which runs on the final manifest after all overrides. `pre_spawn_hook` keeps only what it is allowed to own — the KSA — and stashes the names on the spawner. Both hooks are now wired in `values-gcs.yaml`, and the split is load-bearing.

- **E2 — pre-existing data-loss bug, found only because E1 forced a look at the manifest.** The TCPXO 8-GPU profile supplied `volumes`/`volume_mounts` as **lists**, so by the rule above it *replaced* the chart's dict — deleting the user's home PVC. An 8-GPU pod had only `nvidia`/`aperture-devices`/`shm` and **no `/home/jovyan` mount at all**. The notebook still opened, which is exactly why nobody noticed: every file saved on that profile was lost when the pod was replaced. **Fix:** the profile now uses maps keyed by volume name, so it merges. `test_profile_volume_overrides_are_mappings_not_lists` fails on any profile that regresses this, in any values file in the repo.

- **E3 — `read_only` is not a gcsfuse flag.** The shared-dataset volume set `mountOptions: …,read_only`; the CSI driver forwards each mountOption as `--<flag>`, so the mount failed with `gcsfuse failed with error: Error: unknown flag: --read_only`. The failure mode is nastier than a clean error: the sidecar had already started, so the notebook container retried forever on `transport endpoint is not connected` and the pod sat `Pending` until the spawn timed out. Read-only is a CSI-level concept — `csi.readOnly` plus `readOnly` on the volumeMount, both of which were already set and were sufficient on their own. A unit test had *asserted the broken behaviour*; it is now inverted into a guard that every mountOption is a flag gcsfuse actually has.

- **E4 — the slim CPU image could not open a dataset.** `minimal-notebook` ships no `pandas`, so a freshly downloaded `iris.csv` in `~/gcs` raised `ModuleNotFoundError` — while the Dockerfile's own header claimed `pd.read_csv("gs://…")` worked. Added `pandas` and `pyarrow` (parquet is what most real datasets arrive as) and extended the build-time smoke test to import them. The GPU base already had pandas.
  - Images are now **`v2`**. Tags are bumped, never re-pushed: notebook containers pull `IfNotPresent`, so overwriting a tag leaves every node that cached it running the old image — a rebuild that appears to do nothing. `build.sh` defaults to `v2` and says so.

**All three profiles then verified live, in a real spawn, as the real user identity:**

| Check | CPU (default) | GPU 1x | GPU 8x TCPXO |
|---|---|---|---|
| `~/gcs` writable, owned `1000:100` (`jovyan:users`) | ✅ | — | ✅ |
| `~/shared` present and **read-only** (`touch` → `Read-only file system`) | ✅ | — | ✅ |
| Home PVC `volume-elideng:/home/jovyan` mounted (E2) | ✅ | — | ✅ |
| Write via FUSE → visible via `gcloud storage` / SDK / `gcsfs` | ✅ | — | ✅ |
| `pandas.read_csv("gs://…")` native path | ✅ | — | ✅ |
| Parquet round trip through the mount | ✅ | — | ✅ |
| HTTP dataset download straight into `~/gcs` | ✅ | — | — |
| 512 MB write through the sidecar — 132 MB/s, landed in GCS | ✅ | — | — |
| 8 GPUs + `eth0`–`eth8` + `/dev/aperture_devices` intact | — | — | ✅ |
| **Isolation: another user's bucket denied** (`storage.objects.list` denied) | ✅ | — | ✅ |
| **Isolation: project-wide bucket listing denied** (`storage.buckets.list` denied) | ✅ | — | — |

The 1-GPU profile was not spawned separately: it differs from the 8-GPU profile only in GPU count and in carrying none of the volume overrides that E2 was about, so it is strictly the easier case of a profile already verified.

**Generalisable lesson, and the reason E1/E2 survived a careful review:** every unit test passed, provisioning logged success, and the notebook started. The feature was verified against the object the hook *hands over*, never against the manifest Kubernetes *actually ran*. A spawn that succeeds is not evidence that what you attached is attached.

## Open items

- Sidecar resource values (`gke-gcsfuse/memory-limit` 2Gi, `ephemeral-storage-limit` 100Gi) are reasoned, not measured. A 512 MB write sustained 132 MB/s with no sidecar pressure; still untested against a multi-GB download.
- Images are tagged `v2` off moving base tags, so `v2` is not a reproducible build. Pin bases by digest before relying on it for a published result.
- Base images (`minimal-notebook`, `pytorch-notebook:cuda12-latest`) are moving tags — deliberate for a lab, wrong for anything reproducible.
- Spike leftovers to clean up: bucket `gs://hdlab-elideng-jupyter-asiasoutheast1` with its `users/spiketest*` managed folders, buckets `gs://hdlab-elideng-jupyter-spiketest{,2}`, custom role `jupyterWorkspaceBucketMeta`, KSAs `jupyter-user-spiketest{,2}`, and the finished probe pods.
- The GPU nodes still sit on `1.35.6-gke.1641000` behind the pool target (R3) and auto-upgrade will re-wedge.
- `docs/export/*.html` are generated and were not regenerated for the doc edits.

## Success criteria

1. A user signs in, gets a notebook, and `~/gcs` is their own GCS workspace with no admin action.
2. `curl`/`huggingface-cli` a dataset into `~/gcs`; it persists after the pod dies; it is readable next session.
3. **User A provably cannot list or read user B's workspace** — by FUSE or by SDK.
4. The hub cannot read or write any object in any bucket. **Weakened by the pivot:** still true for object access, but the hub now holds `setIamPolicy` on prefix-matching buckets and so *could* grant itself object access to a workspace. See §3 for why this is unavoidable with auto-provisioning and how it is bounded.
5. The existing TCPXO 8-GPU profile still spawns and still has its 9 interfaces.

Status: **1, 2, 3 and 5 are verified live end-to-end** on 2026-08-27, as the real user identity in a real spawned notebook, on both the CPU and the 8-GPU TCPXO profile — see *Verified findings (live end-to-end, 2026-08-27)*. Criterion 3, the one that actually matters, is confirmed in both directions: another user's bucket is denied `storage.objects.list` and the project is denied `storage.buckets.list`, by SDK and by CLI, from inside the notebook. Criterion 5 holds with the 8 GPUs, `eth0`–`eth8` and the aperture devices all intact — and the profile now *also* keeps its home PVC, which it had been silently dropping before this work (E2).

Criterion 4 is unchanged and remains as qualified above: no hub object access, but the hub holds `setIamPolicy` on prefix-matching buckets.

Getting to that status took four live defects (E1–E4), two of them invisible to a green test suite and a successful spawn.

## Related

- [`deploy/jupyter-gcs/README.md`](../../../deploy/jupyter-gcs/README.md) — the implementation: operator runbook, deployment order, security posture, troubleshooting
- `deploy/jupyter/examples/dataset_to_gcs.ipynb` — worked example: download → land in `~/gcs` → read back → when to stop using FUSE
- `docs/guides/02d-deploy-jupyter.md` §7.7, `docs/guides/04-jupyter-notebook-user-guide.md` §7 — updated for `~/gcs`
- `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` — base values; brace fix and new images landed here (see R4)
- `deploy/ops/node-rotation-runbook.md` — R3 auto-upgrade wedge documented here
