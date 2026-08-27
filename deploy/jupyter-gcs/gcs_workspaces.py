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


def gcsfuse_volume(bucket: str, name: str = "gcs-workspace", read_only: bool = False) -> dict:
    vol = {
        "name": name,
        "csi": {
            "driver": "gcsfuse.csi.storage.gke.io",
            "volumeAttributes": {
                "bucketName": bucket,
                "mountOptions": "implicit-dirs,uid=1000,gid=1000",
            },
        },
    }
    if read_only:
        vol["csi"]["readOnly"] = True
        vol["csi"]["volumeAttributes"]["mountOptions"] += ",read_only"
    return vol


def apply_to_spawner(spawner, names: dict, shared_bucket: str | None = None) -> None:
    """Point the spawner at the user's own identity and mount their bucket.

    Everything here is set as Python objects. Nothing goes through string
    templating on purpose: KubeSpawner runs annotation values through
    ``str.format()``, which is what broke GPU spawns with
    ``KeyError: '"interfaceName"'`` when literal JSON braces reached it.
    """
    spawner.service_account = names["ksa"]

    volumes = list(spawner.volumes or [])
    mounts = list(spawner.volume_mounts or [])

    volumes.append(gcsfuse_volume(names["bucket"]))
    mounts.append({"name": "gcs-workspace", "mountPath": "/home/jovyan/gcs"})

    if shared_bucket:
        volumes.append(gcsfuse_volume(shared_bucket, name="gcs-shared", read_only=True))
        mounts.append(
            {"name": "gcs-shared", "mountPath": "/home/jovyan/shared", "readOnly": True}
        )

    spawner.volumes = volumes
    spawner.volume_mounts = mounts

    annotations = dict(spawner.extra_annotations or {})
    annotations.update(
        {
            "gke-gcsfuse/volumes": "true",
            "gke-gcsfuse/cpu-limit": "500m",
            "gke-gcsfuse/memory-limit": "2Gi",
            # gcsfuse buffers writes to local disk. Too small a limit here is
            # what makes a multi-GB dataset download die partway.
            "gke-gcsfuse/ephemeral-storage-limit": "100Gi",
        }
    )
    spawner.extra_annotations = annotations


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


async def pre_spawn_hook(spawner):  # pragma: no cover - needs cluster + cloud
    username = spawner.user.name
    shared = os.environ.get("GCS_WORKSPACE_SHARED_BUCKET") or None
    try:
        provisioner = build_provisioner()
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
