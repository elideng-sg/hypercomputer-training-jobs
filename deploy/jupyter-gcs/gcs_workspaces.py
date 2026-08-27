"""Per-user GCS workspaces for JupyterHub.

Loaded into the hub pod (see values-gcs.yaml) and wired in as KubeSpawner's
``pre_spawn_hook``. On every spawn it makes sure the user has:

  * a Kubernetes ServiceAccount ``jupyter-user-<name>`` in the hub namespace,
  * a private GCS bucket ``<prefix><name>``,
  * a bucket-level ``roles/storage.objectUser`` binding for that KSA's direct
    Workload Identity principal,

then points the spawner at the KSA and mounts the bucket at ``~/gcs``.

Why a bucket per user rather than one bucket with a folder each: gcsfuse and
the CSI driver both perform a *bucket-level* ``storage.objects.list`` check, and
managed-folder IAM does not satisfy it. Measured live on 2026-08-26 -- a
managed-folder workspace could be read and written but never listed, and the
volume failed to mount at all. A bucket per user is the only shape that gives
working ``ls`` and real isolation at the same time. See
docs/superpowers/specs/2026-08-26-jupyter-gcs-workspaces-design.md.

Dependencies are limited to what the z2jh hub image already ships:
``google.auth``, ``requests``, ``kubernetes_asyncio``. There is no
``google-cloud-storage`` in that image, so the GCS JSON API is called directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time

GCS_API = "https://storage.googleapis.com/storage/v1"
SCOPE = "https://www.googleapis.com/auth/devstorage.full_control"

# A bucket name is capped at 63 characters, so the sanitized username has to fit
# in whatever the prefix leaves behind. Computed against the configured prefix
# rather than hardcoded, so changing the prefix cannot silently produce
# over-long (rejected) bucket names.
BUCKET_NAME_LIMIT = 63
HASH_SUFFIX_LEN = 6  # "-" + 5 hex chars

# Names that must never become a user's workspace. `shared` is the read-only
# common-dataset bucket: a user called "shared" would otherwise be handed
# objectUser on it and could overwrite everyone's datasets. Reserved names get
# the hash suffix, so such a user still gets a workspace, just not that one.
RESERVED_NAMES = frozenset({"shared", "hub", "default", "admin", "public"})

log = logging.getLogger("gcs_workspaces")


class WorkspaceError(Exception):
    """Provisioning failed. Raised to fail a spawn closed rather than hand the
    user a notebook whose ~/gcs is missing or, worse, not theirs."""


def _http_status(exc: Exception):
    """HTTP status carried by a kubernetes_asyncio ApiException, or None if this
    is some other exception (a bug, a timeout) that must not be swallowed."""
    return getattr(exc, "status", None)


def sanitize_username(username: str, max_len: int) -> str:
    """Map a JupyterHub username onto a name usable as both a bucket suffix and
    a KSA name.

    Lowercase, non-alphanumerics to ``-``, collapsed repeats, trimmed to
    ``max_len``. Whenever any of that actually changed the string -- or the
    result is a reserved name -- a short hash of the *original* is appended, so
    that two different usernames cannot normalize onto one workspace (``a.b``
    and ``a-b`` must not collide).
    """
    if not username:
        raise WorkspaceError("empty username")
    if max_len < HASH_SUFFIX_LEN + 1:
        raise WorkspaceError(f"max_len {max_len} too small for a hashed name")

    lowered = username.lower()
    # The "+" collapses runs, so "a..b" and "a__b" both land on "a-b".
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")

    if not cleaned:
        raise WorkspaceError(f"username {username!r} has no usable characters")

    if cleaned == username and len(cleaned) <= max_len and cleaned not in RESERVED_NAMES:
        return cleaned

    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:5]
    base = cleaned[: max_len - HASH_SUFFIX_LEN].rstrip("-")
    return f"{base}-{digest}"


def max_username_len(bucket_prefix: str) -> int:
    return BUCKET_NAME_LIMIT - len(bucket_prefix)


def bucket_name(sanitized: str, bucket_prefix: str) -> str:
    name = f"{bucket_prefix}{sanitized}"
    if len(name) > BUCKET_NAME_LIMIT:
        raise WorkspaceError(f"bucket name {name!r} exceeds {BUCKET_NAME_LIMIT} chars")
    return name


def ksa_name(sanitized: str) -> str:
    return f"jupyter-user-{sanitized}"


def wi_principal(project_number: str, workload_pool: str, namespace: str, ksa: str) -> str:
    """Direct Workload Identity federation principal -- no per-user GSA, so
    there is no service-account key anywhere in this design."""
    return (
        f"principal://iam.googleapis.com/projects/{project_number}"
        f"/locations/global/workloadIdentityPools/{workload_pool}"
        f"/subject/ns/{namespace}/sa/{ksa}"
    )


class WorkspaceProvisioner:
    """Creates the bucket, its IAM binding and the KSA. Idempotent throughout:
    it runs on every single spawn, so "already exists" is the common path.

    ``session`` is anything with ``get``/``post``/``put`` returning a
    requests-like response; ``core_v1`` is a kubernetes_asyncio CoreV1Api. Both
    are injected so the logic is testable without a cluster or a cloud.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
        workload_pool: str,
        namespace: str,
        bucket_prefix: str,
        location: str,
        session,
        core_v1,
        iam_retries: int = 6,
        iam_retry_delay: float = 5.0,
        sleep=time.sleep,
    ):
        self.project = project
        self.project_number = project_number
        self.workload_pool = workload_pool
        self.namespace = namespace
        self.bucket_prefix = bucket_prefix
        self.location = location
        self.session = session
        self.core_v1 = core_v1
        self.iam_retries = iam_retries
        self.iam_retry_delay = iam_retry_delay
        self._sleep = sleep

    # -- naming ---------------------------------------------------------------

    def names_for(self, username: str) -> dict:
        sanitized = sanitize_username(username, max_username_len(self.bucket_prefix))
        ksa = ksa_name(sanitized)
        return {
            "sanitized": sanitized,
            "ksa": ksa,
            "bucket": bucket_name(sanitized, self.bucket_prefix),
            "principal": wi_principal(
                self.project_number, self.workload_pool, self.namespace, ksa
            ),
        }

    # -- GCS ------------------------------------------------------------------

    def _bucket_exists(self, bucket: str) -> bool:
        r = self.session.get(f"{GCS_API}/b/{bucket}")
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        raise WorkspaceError(f"checking bucket {bucket}: HTTP {r.status_code} {r.text}")

    def _create_bucket(self, bucket: str) -> None:
        body = {
            "name": bucket,
            "location": self.location,
            "storageClass": "STANDARD",
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "labels": {"managed-by": "jupyterhub-gcs-workspaces"},
        }
        r = self.session.post(f"{GCS_API}/b", params={"project": self.project}, json=body)
        # 409 means someone else created it between our check and our create.
        if r.status_code == 409:
            log.info("bucket %s already existed on create", bucket)
            return
        if r.status_code not in (200, 201):
            raise WorkspaceError(f"creating bucket {bucket}: HTTP {r.status_code} {r.text}")
        log.info("created bucket %s", bucket)

    def _ensure_bucket_iam(self, bucket: str, principal: str) -> bool:
        """Grant objectUser on the whole bucket to this one principal.

        Bucket-level (not prefix-conditioned) is deliberate: it is what makes
        ``storage.objects.list`` work, which is the thing managed folders could
        not deliver. Isolation comes from the bucket being the user's alone.

        Returns True if a binding was added, False if it was already there.
        """
        role = "roles/storage.objectUser"
        r = self.session.get(
            f"{GCS_API}/b/{bucket}/iam", params={"optionsRequestedPolicyVersion": 3}
        )
        if r.status_code != 200:
            raise WorkspaceError(f"reading IAM for {bucket}: HTTP {r.status_code} {r.text}")
        policy = r.json()
        bindings = policy.setdefault("bindings", [])

        for b in bindings:
            if b.get("role") == role and principal in b.get("members", []):
                return False

        bindings.append({"role": role, "members": [principal]})
        policy["version"] = 3
        put = self.session.put(f"{GCS_API}/b/{bucket}/iam", json=policy)
        if put.status_code not in (200, 201):
            raise WorkspaceError(f"setting IAM for {bucket}: HTTP {put.status_code} {put.text}")
        log.info("granted %s on %s to %s", role, bucket, principal)
        return True

    def _wait_for_iam(self, bucket: str, principal: str) -> None:
        """IAM is eventually consistent; a fresh binding can 403 for a while and
        the pod would fail to mount. Confirm the binding is readable back before
        letting the spawn continue."""
        for attempt in range(1, self.iam_retries + 1):
            r = self.session.get(
                f"{GCS_API}/b/{bucket}/iam", params={"optionsRequestedPolicyVersion": 3}
            )
            if r.status_code == 200:
                for b in r.json().get("bindings", []):
                    if principal in b.get("members", []):
                        return
            if attempt < self.iam_retries:
                self._sleep(self.iam_retry_delay)
        raise WorkspaceError(
            f"IAM binding for {principal} on {bucket} did not converge after "
            f"{self.iam_retries} checks"
        )

    def ensure_storage(self, username: str) -> dict:
        """Blocking half (GCS REST). Kept separate from the async half so it can
        be pushed to a thread and not stall the hub's event loop."""
        names = self.names_for(username)
        bucket, principal = names["bucket"], names["principal"]

        if not self._bucket_exists(bucket):
            self._create_bucket(bucket)

        if self._ensure_bucket_iam(bucket, principal):
            self._wait_for_iam(bucket, principal)

        return names

    # -- Kubernetes -----------------------------------------------------------

    async def ensure_ksa(self, username: str, names: dict) -> None:
        """Create the user's KSA if it is missing.

        The API is driven with a plain dict body and the response code is read
        off the exception by attribute rather than by catching ``ApiException``,
        so this module imports nothing from ``kubernetes_asyncio`` and the tests
        need no cluster client installed.
        """
        ksa = names["ksa"]
        try:
            await self.core_v1.read_namespaced_service_account(ksa, self.namespace)
            return
        except Exception as exc:
            if _http_status(exc) != 404:
                raise WorkspaceError(f"reading KSA {ksa}: {exc}") from exc

        body = {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": ksa,
                "namespace": self.namespace,
                # The sanitized name is lossy, so keep the original for humans
                # doing forensics on who owns which bucket.
                "annotations": {"lab.hdlab/jupyterhub-username": username},
                "labels": {"app": "jupyterhub", "component": "gcs-workspace"},
            },
        }
        try:
            await self.core_v1.create_namespaced_service_account(self.namespace, body)
            log.info("created KSA %s for %r", ksa, username)
        except Exception as exc:
            if _http_status(exc) != 409:  # 409 = lost a race with a concurrent spawn
                raise WorkspaceError(f"creating KSA {ksa}: {exc}") from exc

    async def ensure(self, username: str) -> dict:
        names = await asyncio.get_running_loop().run_in_executor(
            None, self.ensure_storage, username
        )
        await self.ensure_ksa(username, names)
        return names


# -- spawner wiring -----------------------------------------------------------


# The mount has to be owned by the identity the notebook actually runs as. z2jh
# 4.4.0 defaults to singleuser.uid=1000 / singleuser.fsGid=100 (jovyan:users in
# the docker-stacks images), and neither values file overrides them. Getting the
# gid wrong is quietly survivable -- the uid still matches, so owner bits carry
# the day -- which is exactly why it is worth pinning correctly here rather than
# discovering it from a group-permission bug later.
MOUNT_UID = 1000
MOUNT_GID = 100


def gcsfuse_volume(bucket: str, name: str = "gcs-workspace", read_only: bool = False) -> dict:
    vol = {
        "name": name,
        "csi": {
            "driver": "gcsfuse.csi.storage.gke.io",
            "volumeAttributes": {
                "bucketName": bucket,
                "mountOptions": f"implicit-dirs,uid={MOUNT_UID},gid={MOUNT_GID}",
            },
        },
    }
    if read_only:
        # Read-only is a CSI-level concept here, NOT a mountOption. The driver
        # forwards every comma-separated mountOption to gcsfuse as a `--flag`, and
        # gcsfuse has no `read_only` flag: adding one makes the mount fail with
        #     gcsfuse failed with error: Error: unknown flag: --read_only
        # and the notebook container then wedges in a retry loop on
        # "transport endpoint is not connected" rather than failing outright.
        # Observed live on 2026-08-27. `csi.readOnly` (plus readOnly on the
        # volumeMount, set by the caller) is what actually enforces it.
        vol["csi"]["readOnly"] = True
    return vol


WORKSPACE_VOLUME = "gcs-workspace"
SHARED_VOLUME = "gcs-shared"
HOME = "/home/jovyan"

FUSE_ANNOTATIONS = {
    "gke-gcsfuse/volumes": "true",
    "gke-gcsfuse/cpu-limit": "500m",
    "gke-gcsfuse/memory-limit": "2Gi",
    # gcsfuse buffers writes to local disk. Too small a limit here is what makes
    # a multi-GB dataset download die partway.
    "gke-gcsfuse/ephemeral-storage-limit": "100Gi",
}


def apply_to_spawner(spawner, names: dict, shared_bucket: str | None = None) -> None:
    """Point the spawner at the user's own identity, and stash the names.

    Deliberately does NOT touch ``spawner.volumes``. Two independent reasons,
    both learned from a live spawn that came up with no ``~/gcs`` at all:

    1. KubeSpawner applies a profile's ``kubespawner_override`` *after*
       ``pre_spawn_hook``. A profile that supplies ``volumes`` as a list (the
       TCPXO one does) replaces whatever this hook had put there.
    2. z2jh sets ``c.KubeSpawner.volumes`` to a **dict** keyed by volume name so
       that overrides merge. ``list(spawner.volumes)`` on a dict silently yields
       the *key strings*, which would corrupt the volume list rather than fail.

    The mounts are therefore injected by ``modify_pod_hook`` instead, which runs
    on the finished manifest and is immune to both.
    """
    spawner.service_account = names["ksa"]
    spawner._gcs_workspace = {"names": names, "shared": shared_bucket}


def _vol_name(entry) -> str | None:
    """Name of a volume/mount that may be a dict or a kubernetes model object."""
    if isinstance(entry, dict):
        return entry.get("name")
    return getattr(entry, "name", None)


def apply_to_pod(pod, names: dict, shared_bucket: str | None = None,
                 container_name: str = "notebook") -> None:
    """Inject the gcsfuse volumes into the pod that is about to be submitted.

    Appends plain dicts alongside whatever model objects KubeSpawner already
    built; the kubernetes client serializes a mixed list element by element, so
    the dict keys here are camelCase to match what the API expects.
    """
    container = next(
        (c for c in (pod.spec.containers or []) if _vol_name(c) == container_name), None
    )
    if container is None:
        # Fail closed rather than return a pod whose ~/gcs is quietly absent.
        have = [_vol_name(c) for c in (pod.spec.containers or [])]
        raise WorkspaceError(f"no {container_name!r} container in pod; found {have}")

    volumes = list(pod.spec.volumes or [])
    mounts = list(container.volume_mounts or [])
    existing = {_vol_name(v) for v in volumes}

    wanted = [(WORKSPACE_VOLUME, names["bucket"], f"{HOME}/gcs", False)]
    if shared_bucket:
        wanted.append((SHARED_VOLUME, shared_bucket, f"{HOME}/shared", True))

    for vol_name, bucket, path, read_only in wanted:
        if vol_name in existing:  # already injected -- keep this idempotent
            continue
        volumes.append(gcsfuse_volume(bucket, name=vol_name, read_only=read_only))
        mount = {"name": vol_name, "mountPath": path}
        if read_only:
            mount["readOnly"] = True
        mounts.append(mount)

    pod.spec.volumes = volumes
    container.volume_mounts = mounts

    annotations = dict(pod.metadata.annotations or {})
    annotations.update(FUSE_ANNOTATIONS)
    pod.metadata.annotations = annotations


def _env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise WorkspaceError(f"required env var {name} is not set")
    return val


def build_provisioner():  # pragma: no cover - needs cluster + cloud
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from kubernetes_asyncio import client, config

    credentials, _ = google.auth.default(scopes=[SCOPE])
    config.load_incluster_config()

    return WorkspaceProvisioner(
        project=_env("GCS_WORKSPACE_PROJECT"),
        project_number=_env("GCS_WORKSPACE_PROJECT_NUMBER"),
        workload_pool=_env("GCS_WORKSPACE_WORKLOAD_POOL"),
        namespace=_env("POD_NAMESPACE", "jupyter"),
        bucket_prefix=_env("GCS_WORKSPACE_BUCKET_PREFIX"),
        location=_env("GCS_WORKSPACE_LOCATION"),
        session=AuthorizedSession(credentials),
        core_v1=client.CoreV1Api(),
    )


_PROVISIONER = None


def get_provisioner():
    """One provisioner for the life of the hub process.

    Not just an optimisation. ``client.CoreV1Api()`` builds an ApiClient that
    owns an ``aiohttp.ClientSession``, and nothing closes it -- building one per
    spawn leaks a session and its sockets every time anyone starts a server, in a
    process that is meant to run for months. Reusing it also stops us
    re-resolving credentials against the metadata server on every spawn.

    No lock: the hook runs on the hub's single event loop and
    ``build_provisioner`` is synchronous, so there is no await between the check
    and the assignment for a second spawn to interleave into.
    """
    global _PROVISIONER
    if _PROVISIONER is None:
        _PROVISIONER = build_provisioner()
    return _PROVISIONER


async def pre_spawn_hook(spawner):  # pragma: no cover - needs cluster + cloud
    username = spawner.user.name
    shared = os.environ.get("GCS_WORKSPACE_SHARED_BUCKET") or None
    try:
        provisioner = get_provisioner()
        names = await provisioner.ensure(username)
        apply_to_spawner(spawner, names, shared_bucket=shared)
    except Exception as exc:
        # Fail closed. A notebook without its workspace looks fine until the
        # user has lost work, so refusing the spawn is the kinder outcome.
        spawner.log.exception("GCS workspace provisioning failed for %r", username)
        raise WorkspaceError(
            f"Could not prepare your GCS workspace: {exc}. "
            "Your notebook was not started -- tell the lab admin."
        ) from exc
    spawner.log.info(
        "GCS workspace ready for %r: bucket=%s ksa=%s", username, names["bucket"], names["ksa"]
    )


async def modify_pod_hook(spawner, pod):  # pragma: no cover - exercised via apply_to_pod
    """Inject the mounts into the final manifest. MUST return the pod."""
    stash = getattr(spawner, "_gcs_workspace", None)
    if not stash:
        # pre_spawn_hook is what fills this in, and it raises on failure, so an
        # empty stash means the hooks are misconfigured rather than that the user
        # has no workspace. Refuse instead of starting a pod without ~/gcs.
        raise WorkspaceError(
            "GCS workspace names were never stashed -- is pre_spawn_hook wired up? "
            "Your notebook was not started -- tell the lab admin."
        )
    try:
        apply_to_pod(pod, stash["names"], shared_bucket=stash["shared"])
    except Exception as exc:
        spawner.log.exception("GCS workspace mount injection failed")
        raise WorkspaceError(f"Could not attach your GCS workspace: {exc}") from exc
    return pod
