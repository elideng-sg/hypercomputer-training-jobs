# Per-User GCS Workspaces for JupyterHub on TCPXO — Design Spec

- **Date:** 2026-08-26
- **Status:** Approved (design); spec pending review → implementation plan
- **Cluster:** `hypercomputer-a3-tcpxo`, zone `asia-southeast1-c`, project `hdlab-elideng` (project number `151935633952`)
- **Branch:** `feat/jupyter-gcs-workspaces`, based on `worktree-tcpxo-migration` (**not** `main` — the TCPXO JupyterHub config exists only on that branch)
- **Depends on:** live JupyterHub (z2jh chart 4.4.0, release `jhub`, ns `jupyter`, Google OAuth), Workload Identity pool `hdlab-elideng.svc.id.goog`

## Goal

Let JupyterHub users download their own datasets and keep them in Cloud Storage, use them from notebooks as ordinary files, and have an **isolated per-user workspace** in GCS for all their artifacts — isolated by IAM, not merely by convention.

Today a notebook has no cloud identity at all: user pods run as the shared `default` ServiceAccount and an init container iptables-DROPs the metadata server, so no GCS access of any kind is possible.

## Decisions (from brainstorming)

| Dimension | Decision |
|-----------|----------|
| Isolation | **One bucket + one GCS managed folder per user**, per-folder IAM (list included) |
| Provisioning | **Hub auto-provisions on first login** (`pre_spawn_hook`), no admin step |
| Access path | **FUSE mount *and* CLI/SDK** — `~/gcs` for filesystem workflows, `gcloud storage`/`gcsfs` for bulk |
| Bucket | **New regional bucket in `asia-southeast1`**, co-located with the cluster |
| Per-user identity | One KSA per user + **direct Workload Identity federation** (no GSA per user) |
| Hierarchical namespace | **Off** — keeps managed folders unambiguous (see Verified findings) |
| Deprovisioning | **Out of scope.** Nothing is ever deleted automatically |

## 1. Storage layout

Bucket `gs://hdlab-elideng-jupyter-asiasoutheast1` — region `asia-southeast1`, uniform bucket-level access, public access prevention enforced, HNS off.

```
gs://hdlab-elideng-jupyter-asiasoutheast1/
├── users/<username>/     managed folder, one per user — private workspace
└── shared/               managed folder — read-only to all users, admin-writable
```

"Admin-writable" means a human with project-level `roles/storage.admin` populates `shared/` by hand. No automation writes there, and neither the hub nor any user KSA can.

`users/<username>/` holds datasets, checkpoints and any other artifacts. `shared/` exists so a large public dataset is downloaded once rather than once per user.

**Username sanitization.** The JupyterHub username becomes both a KSA name and a folder name, so it is normalized: lowercase, non-conforming characters to `-`, collapse repeats, strip leading/trailing `-`, truncate to 57 chars, and append `-` + first 5 hex of the SHA-256 of the original name **whenever normalization changed anything** (so `a.b` and `a-b` cannot collide). The mapping is recorded as a KSA annotation `lab.hdlab/jupyterhub-username` holding the original name. Current users (`elideng`, `samaujs`, `kzuo`) pass through unchanged.

## 2. Per-user identity

- KSA `jupyter-user-<sanitized>` in ns `jupyter`. KubeSpawner sets `service_account` per spawn.
- Direct WI federation — **no per-user GSA**, no key material:
  ```
  principal://iam.googleapis.com/projects/151935633952/locations/global/workloadIdentityPools/hdlab-elideng.svc.id.goog/subject/ns/jupyter/sa/jupyter-user-<sanitized>
  ```
- Grants for that principal:

| Resource | Role | Why |
|---|---|---|
| managed folder `users/<u>/` | `roles/storage.objectUser` | read/write/delete **and list** within their own folder |
| managed folder `shared/` | `roles/storage.objectViewer` | read common datasets |
| bucket (whole) | custom `jupyterWorkspaceBucketMeta` = **`storage.buckets.get` only** | gcsfuse needs bucket metadata at mount time; exposes no object names |

The bucket-level grant is deliberately the narrowest thing that lets gcsfuse start. Granting a normal bucket-level reader role instead would hand every user a listing of everyone's data and defeat the whole design.

## 3. Hub auto-provisioning (the privileged component)

Chosen for self-service, so the hub needs a GCP identity. It is boxed in tightly:

- Hub KSA `hub` → WI → GSA `jhub-provisioner@hdlab-elideng.iam.gserviceaccount.com`.
- That GSA is bound **only on this one bucket** to custom role `jupyterWorkspaceProvisioner`:
  `storage.managedFolders.create`, `.get`, `.list`, `.getIamPolicy`, `.setIamPolicy`.
- Consequently the hub **cannot read or write a single object**, cannot touch another bucket, cannot create buckets, and holds nothing at project level.
- Hub's k8s Role gains `serviceaccounts: [get, create]` — no `delete`, no `patch`.

`pre_spawn_hook` (in `hub.extraConfig`), idempotent, runs on every spawn:

1. Sanitize the username.
2. Ensure KSA exists (create if absent, annotated with the original username).
3. Ensure managed folder `users/<u>/` exists.
4. Ensure the folder IAM binding exists (read-modify-write on the folder policy, preserving other bindings).
5. Verify effective access with a bounded retry — `testIamPermissions` on the folder for the user's principal, retried with backoff up to a fixed ceiling (IAM propagation is not instant; the first mount can otherwise 403).
6. Set `spawner.service_account` and attach the two CSI volumes with `only-dir` computed **in Python**, never via string templating.

**Fails closed.** If any step fails the spawn aborts with an actionable message. Handing a user a notebook whose `~/gcs` silently is not theirs — or is someone else's — is the one outcome worth failing a spawn to avoid.

**Never deprovisions.** Removing a user is a deliberate admin action, documented in the runbook, not something an internet-facing hub can do.

## 4. Notebook side

- **GCS FUSE CSI driver** enabled on the cluster (done — see Verified findings).
- **`singleuser.cloudMetadata.blockWithIptables: false`** — mandatory. Nothing can authenticate to GCS while 169.254.169.254 is DROPped, and the block applies to the whole pod network namespace, so it disables the gcsfuse sidecar too.

  This is the one security-posture change in the design, and it is only acceptable *because* of §2: the identity a user can now reach through the metadata server is their own folder-scoped KSA. Before this change the reachable identity would have been the shared `default` SA, which is exactly why the block was there. **The block must not be lifted before per-user KSAs are in place** — the ordering is a correctness requirement, not a preference.
- Mounts (CSI inline ephemeral volumes, `gcsfuse.csi.storage.gke.io`):

| Path | Contents | Options |
|---|---|---|
| `/home/jovyan/gcs` | their workspace | `only-dir=users/<u>`, `implicit-dirs`, uid/gid 1000 |
| `/home/jovyan/shared` | common datasets | `only-dir=shared`, `read_only: true` |

- **The 20Gi `premium-rwo` PVC stays as `/home/jovyan`.** Code, notebooks, git checkouts and in-progress checkpoints belong on a real disk; GCS holds data and finished artifacts. Putting the home directory itself on FUSE breaks Jupyter's checkpoint/atomic-rename behavior.
- Sidecar gets explicit resources via annotations (`gke-gcsfuse/cpu-limit`, `memory-limit`, `ephemeral-storage-limit`). The **ephemeral-storage limit is what makes multi-GB dataset downloads work** — gcsfuse buffers writes to local disk, and the default is too small for a 50GB download.
- New image `asia-southeast1-docker.pkg.dev/hdlab-elideng/lab-images/pytorch-notebook-gcs:<tag>` = `quay.io/jupyter/pytorch-notebook:cuda12-latest` + `gcloud` CLI + `gcsfs` + `google-cloud-storage` + `huggingface_hub[cli]` + `kaggle`. The current image has none of these. Added to `prePuller.extraImages`.
  A **new Artifact Registry repo in `asia-southeast1`** — the existing `lab-images` is in `asia-east1`, and this is a ~10GB CUDA image pulled on every new node.
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

Pure logic, unit-tested with pytest (no cluster needed):
- username sanitizer: idempotence, collision resistance, length, annotation round-trip
- hook logic against a faked GCS/k8s client: creates-when-absent, no-op-when-present, fails-closed-on-error
- `test_profile_annotations.py` — cherry-picked from `fix/jupyter-tcpxo-spawn` (it does not exist on this branch), then extended to cover the new mounts and profiles (see R4)

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
- **R4 — pre-existing repo defect, in a file this work must edit.** The live hub runs the TCPXO interfaces annotation with **doubled** braces; `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` on the tip branch has **single** braces, which reproduces the production `KeyError: '"interfaceName"'` (kubespawner runs annotation values through `str.format()`). The fix and its test exist only on the stale side branch `fix/jupyter-tcpxo-spawn`, which is 7 commits behind. Two files describe the same config and the newer one is the broken one. This work will fix the braces and consolidate to a single source of truth that matches live, and is the reason the hook computes `only-dir` in Python rather than by templating.

## Open items

- **R1 (crux) — not yet verified:** that gcsfuse mounts successfully with *folder-scoped* IAM plus `only-dir`, given object listing is normally a bucket-level permission. Managed folders exist precisely to scope listing, so this is expected to work, but it is unproven and needs a live pod. **If it fails, the fallback is one bucket per user** — a different design that changes §1–§3, and requires going back to the user, not a silent substitution.
- Whether gcsfuse additionally demands `storage.objects.list` at bucket level. If it does, the only isolation-preserving answer is the per-user-bucket fallback, since a bucket-level list grant exposes every user's object names.
- Sidecar resource values (memory / ephemeral-storage) to be tuned against a real multi-GB download rather than guessed.

## Success criteria

1. A user signs in, gets a notebook, and `~/gcs` is their own GCS workspace with no admin action.
2. `curl`/`huggingface-cli` a dataset into `~/gcs`; it persists after the pod dies; it is readable next session.
3. **User A provably cannot list or read user B's workspace** — by FUSE or by SDK.
4. The hub cannot read or write any object in the bucket.
5. The existing TCPXO 8-GPU profile still spawns and still has its 9 interfaces.

## Related

- `docs/guides/02d-deploy-jupyter.md`, `docs/guides/04-jupyter-notebook-user-guide.md` — need updating for `~/gcs`
- `deploy/tcpxo-migration/03-jupyter-values-tcpxo.yaml` — the values file this work edits (see R4)
- `deploy/ops/node-rotation-runbook.md` — home for the R3 auto-upgrade note
