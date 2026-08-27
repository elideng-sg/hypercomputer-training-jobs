"""Unit tests for gcs_workspaces.

No cluster and no cloud: the GCS JSON API and the Kubernetes client are both
faked. Run from this directory:

    pytest deploy/jupyter-gcs/ -q

The tests that matter most are the ones asserting *isolation* invariants -- that
two different usernames can never share a bucket, and that a failure anywhere in
provisioning propagates instead of yielding a half-configured spawner.
"""

import asyncio
import copy

import pytest

import gcs_workspaces
from gcs_workspaces import (
    BUCKET_NAME_LIMIT,
    RESERVED_NAMES,
    WorkspaceError,
    WorkspaceProvisioner,
    apply_to_pod,
    apply_to_spawner,
    bucket_name,
    gcsfuse_volume,
    ksa_name,
    max_username_len,
    sanitize_username,
    wi_principal,
)

PREFIX = "hdlab-elideng-jupyter-"
MAXLEN = max_username_len(PREFIX)


# -- sanitizer ----------------------------------------------------------------


@pytest.mark.parametrize("username", ["elideng", "samaujs", "kzuo"])
def test_current_users_pass_through_unchanged(username):
    """The three real users must keep the plain names used in the runbooks."""
    assert sanitize_username(username, MAXLEN) == username


@pytest.mark.parametrize(
    "username",
    ["Elideng", "eli.deng", "eli_deng", "eli deng", "-elideng", "eli@google.com"],
)
def test_names_needing_normalization_get_a_hash_suffix(username):
    got = sanitize_username(username, MAXLEN)
    assert got != username
    # "-" + 5 hex chars
    suffix = got.rsplit("-", 1)[-1]
    assert len(suffix) == 5 and all(c in "0123456789abcdef" for c in suffix)


def test_distinct_usernames_never_collide():
    """The whole isolation story rests on this: if two usernames mapped to one
    bucket, one user would silently get another user's data."""
    colliding_pairs = [
        ("a.b", "a-b"),
        ("a.b", "a_b"),
        ("Alice", "alice"),
        ("a..b", "a.b"),
        ("bob", "BOB"),
    ]
    for left, right in colliding_pairs:
        assert sanitize_username(left, MAXLEN) != sanitize_username(right, MAXLEN)


@pytest.mark.parametrize("reserved", sorted(RESERVED_NAMES))
def test_reserved_names_cannot_become_a_user_workspace(reserved):
    """A user called "shared" must not be handed the shared read-only bucket --
    they would get objectUser on everyone's common datasets."""
    got = sanitize_username(reserved, MAXLEN)
    assert got != reserved
    assert bucket_name(got, PREFIX) != f"{PREFIX}{reserved}"


def test_sanitizing_an_already_sanitized_name_is_stable():
    """Idempotence for the pass-through case, so a name in a runbook stays valid."""
    once = sanitize_username("elideng", MAXLEN)
    assert sanitize_username(once, MAXLEN) == once


def test_long_name_is_truncated_and_still_fits_the_bucket_limit():
    long = "x" * 200
    got = sanitize_username(long, MAXLEN)
    assert len(got) <= MAXLEN
    assert len(bucket_name(got, PREFIX)) <= BUCKET_NAME_LIMIT


def test_truncated_names_that_share_a_prefix_stay_distinct():
    a = sanitize_username("y" * 100 + "a", MAXLEN)
    b = sanitize_username("y" * 100 + "b", MAXLEN)
    assert a != b


def test_sanitized_name_is_a_legal_bucket_suffix():
    for username in ["Eli.Deng", "x" * 100, "a--b", "9lives"]:
        got = sanitize_username(username, MAXLEN)
        assert got[0].isalnum() and got[-1].isalnum(), got
        assert all(c.islower() or c.isdigit() or c == "-" for c in got), got


@pytest.mark.parametrize("bad", ["", "...", "@@@", "-", "___"])
def test_unusable_usernames_are_rejected(bad):
    with pytest.raises(WorkspaceError):
        sanitize_username(bad, MAXLEN)


def test_max_len_too_small_is_rejected_rather_than_producing_a_bad_name():
    with pytest.raises(WorkspaceError):
        sanitize_username("elideng", 3)


def test_bucket_name_over_the_limit_is_rejected():
    with pytest.raises(WorkspaceError):
        bucket_name("z" * 60, PREFIX)


def test_principal_is_the_direct_workload_identity_form():
    got = wi_principal("151935633952", "hdlab-elideng.svc.id.goog", "jupyter", "jupyter-user-eli")
    assert got == (
        "principal://iam.googleapis.com/projects/151935633952/locations/global"
        "/workloadIdentityPools/hdlab-elideng.svc.id.goog/subject/ns/jupyter"
        "/sa/jupyter-user-eli"
    )
    # A GSA-shaped member would mean key material somewhere; there must be none.
    assert "gserviceaccount.com" not in got


# -- fakes --------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeGcs:
    """Minimal stand-in for an AuthorizedSession against the GCS JSON API."""

    def __init__(self, existing_buckets=None, policies=None):
        self.buckets = set(existing_buckets or [])
        self.policies = dict(policies or {})
        self.calls = []
        self.fail_iam_put = False
        self.iam_never_converges = False

    def get(self, url, params=None):
        self.calls.append(("GET", url))
        if url.endswith("/iam"):
            bucket = url.split("/b/")[1].split("/")[0]
            if bucket not in self.buckets:
                return FakeResponse(404)
            # Deep copy: a real response is a fresh parse, so the caller
            # mutating it must not write through to stored state.
            policy = self.policies.get(bucket, {"bindings": [], "etag": "e0"})
            return FakeResponse(200, copy.deepcopy(policy))
        bucket = url.split("/b/")[1]
        return FakeResponse(200 if bucket in self.buckets else 404)

    def post(self, url, params=None, json=None):
        self.calls.append(("POST", url, json))
        self.buckets.add(json["name"])
        self.policies.setdefault(json["name"], {"bindings": [], "etag": "e0"})
        return FakeResponse(200, json)

    def put(self, url, json=None):
        self.calls.append(("PUT", url, json))
        if self.fail_iam_put:
            return FakeResponse(403, {"error": "denied"})
        bucket = url.split("/b/")[1].split("/")[0]
        if not self.iam_never_converges:
            self.policies[bucket] = json
        return FakeResponse(200, json)


class FakeApiError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class FakeCoreV1:
    def __init__(self, existing=(), create_error=None, read_error=None):
        self.existing = set(existing)
        self.created = []
        self.create_error = create_error
        self.read_error = read_error

    async def read_namespaced_service_account(self, name, namespace):
        if self.read_error is not None:
            raise self.read_error
        if name not in self.existing:
            raise FakeApiError(404)
        return {"metadata": {"name": name}}

    async def create_namespaced_service_account(self, namespace, body):
        if self.create_error is not None:
            raise self.create_error
        self.created.append(body)
        self.existing.add(body["metadata"]["name"])
        return body


def make_provisioner(gcs=None, core_v1=None, **kw):
    slept = []
    p = WorkspaceProvisioner(
        project="hdlab-elideng",
        project_number="151935633952",
        workload_pool="hdlab-elideng.svc.id.goog",
        namespace="jupyter",
        bucket_prefix=PREFIX,
        location="asia-southeast1",
        session=gcs if gcs is not None else FakeGcs(),
        core_v1=core_v1 if core_v1 is not None else FakeCoreV1(),
        iam_retry_delay=0,
        sleep=slept.append,
        **kw,
    )
    p.slept = slept
    return p


# -- naming through the provisioner -------------------------------------------


def test_names_for_is_self_consistent():
    p = make_provisioner()
    names = p.names_for("elideng")
    assert names["bucket"] == "hdlab-elideng-jupyter-elideng"
    assert names["ksa"] == ksa_name("elideng") == "jupyter-user-elideng"
    assert names["ksa"] in names["principal"]


# -- bucket provisioning ------------------------------------------------------


def test_creates_bucket_when_absent_with_ubla_and_pap_enforced():
    gcs = FakeGcs()
    p = make_provisioner(gcs)
    p.ensure_storage("newuser")

    creates = [c for c in gcs.calls if c[0] == "POST"]
    assert len(creates) == 1
    body = creates[0][2]
    assert body["name"] == "hdlab-elideng-jupyter-newuser"
    assert body["location"] == "asia-southeast1"
    # A workspace bucket that is world-readable or ACL-managed would break
    # isolation, so both are pinned at creation time.
    assert body["iamConfiguration"]["uniformBucketLevelAccess"]["enabled"] is True
    assert body["iamConfiguration"]["publicAccessPrevention"] == "enforced"


def test_does_not_recreate_an_existing_bucket():
    gcs = FakeGcs(existing_buckets={"hdlab-elideng-jupyter-elideng"})
    p = make_provisioner(gcs)
    p.ensure_storage("elideng")
    assert not [c for c in gcs.calls if c[0] == "POST"]


def test_grants_object_user_to_exactly_the_users_principal():
    gcs = FakeGcs()
    p = make_provisioner(gcs)
    names = p.ensure_storage("elideng")

    puts = [c for c in gcs.calls if c[0] == "PUT"]
    assert len(puts) == 1
    bindings = puts[0][2]["bindings"]
    assert {"role": "roles/storage.objectUser", "members": [names["principal"]]} in bindings
    # Nobody else gets access as a side effect.
    for b in bindings:
        assert b["members"] == [names["principal"]]


def test_iam_write_preserves_pre_existing_bindings():
    """Read-modify-write, not overwrite: clobbering the admin binding would lock
    the owners out of their own bucket."""
    admin = {"role": "roles/storage.admin", "members": ["user:admin@google.com"]}
    gcs = FakeGcs(
        existing_buckets={"hdlab-elideng-jupyter-elideng"},
        policies={"hdlab-elideng-jupyter-elideng": {"bindings": [admin], "etag": "e1"}},
    )
    p = make_provisioner(gcs)
    p.ensure_storage("elideng")
    put = [c for c in gcs.calls if c[0] == "PUT"][0]
    assert admin in put[2]["bindings"]
    assert put[2]["etag"] == "e1"  # optimistic concurrency preserved


def test_second_spawn_is_a_no_op():
    """The hook runs on every spawn; the steady state must be read-only."""
    gcs = FakeGcs()
    p = make_provisioner(gcs)
    p.ensure_storage("elideng")
    gcs.calls.clear()
    p.ensure_storage("elideng")
    assert [c[0] for c in gcs.calls] == ["GET", "GET"]  # bucket check + iam check only


def test_bucket_check_failure_propagates():
    gcs = FakeGcs()
    gcs.get = lambda url, params=None: FakeResponse(500, {"error": "boom"})
    p = make_provisioner(gcs)
    with pytest.raises(WorkspaceError, match="checking bucket"):
        p.ensure_storage("elideng")


def test_iam_set_failure_propagates():
    gcs = FakeGcs()
    gcs.fail_iam_put = True
    p = make_provisioner(gcs)
    with pytest.raises(WorkspaceError, match="setting IAM"):
        p.ensure_storage("elideng")


def test_waits_for_iam_to_converge_then_proceeds():
    gcs = FakeGcs()
    p = make_provisioner(gcs)
    p.ensure_storage("elideng")
    # Binding was visible on the first read-back, so no backoff was needed.
    assert p.slept == []


def test_gives_up_when_iam_never_converges():
    """Better a failed spawn than a notebook whose mount 403s minutes later."""
    gcs = FakeGcs()
    gcs.iam_never_converges = True
    p = make_provisioner(gcs, iam_retries=3)
    with pytest.raises(WorkspaceError, match="did not converge"):
        p.ensure_storage("elideng")
    assert len(p.slept) == 2  # retries - 1 waits


# -- KSA provisioning ---------------------------------------------------------


def test_creates_ksa_with_the_original_username_recorded():
    core = FakeCoreV1()
    p = make_provisioner(core_v1=core)
    names = p.names_for("Eli.Deng")
    asyncio.run(p.ensure_ksa("Eli.Deng", names))

    assert len(core.created) == 1
    meta = core.created[0]["metadata"]
    assert meta["name"] == names["ksa"]
    assert meta["namespace"] == "jupyter"
    # The sanitized name is lossy; the annotation is how an admin maps a bucket
    # back to a human.
    assert meta["annotations"]["lab.hdlab/jupyterhub-username"] == "Eli.Deng"


def test_existing_ksa_is_left_alone():
    core = FakeCoreV1(existing={"jupyter-user-elideng"})
    p = make_provisioner(core_v1=core)
    asyncio.run(p.ensure_ksa("elideng", p.names_for("elideng")))
    assert core.created == []


def test_ksa_create_race_is_tolerated():
    core = FakeCoreV1(create_error=FakeApiError(409))
    p = make_provisioner(core_v1=core)
    asyncio.run(p.ensure_ksa("elideng", p.names_for("elideng")))  # no raise


def test_ksa_permission_error_fails_closed():
    core = FakeCoreV1(create_error=FakeApiError(403))
    p = make_provisioner(core_v1=core)
    with pytest.raises(WorkspaceError, match="creating KSA"):
        asyncio.run(p.ensure_ksa("elideng", p.names_for("elideng")))


def test_ksa_read_error_that_is_not_404_fails_closed():
    core = FakeCoreV1(read_error=FakeApiError(500))
    p = make_provisioner(core_v1=core)
    with pytest.raises(WorkspaceError, match="reading KSA"):
        asyncio.run(p.ensure_ksa("elideng", p.names_for("elideng")))


def test_non_api_exception_is_not_swallowed():
    """A TypeError from our own code must not be mistaken for a 404."""
    core = FakeCoreV1(read_error=TypeError("bug"))
    p = make_provisioner(core_v1=core)
    with pytest.raises(WorkspaceError):
        asyncio.run(p.ensure_ksa("elideng", p.names_for("elideng")))


def test_ensure_does_storage_then_ksa():
    gcs, core = FakeGcs(), FakeCoreV1()
    p = make_provisioner(gcs, core)
    names = asyncio.run(p.ensure("elideng"))
    assert names["bucket"] in gcs.buckets
    assert core.created[0]["metadata"]["name"] == names["ksa"]


# -- spawner wiring -----------------------------------------------------------


class FakeSpawner:
    def __init__(self, volumes=None, volume_mounts=None, extra_annotations=None):
        self.volumes = volumes
        self.volume_mounts = volume_mounts
        self.extra_annotations = extra_annotations
        self.service_account = None


class FakeContainer:
    """Stands in for a V1Container: attribute access, snake_case volume_mounts."""

    def __init__(self, name, volume_mounts=None):
        self.name = name
        self.volume_mounts = volume_mounts


class _Spec:
    def __init__(self, containers, volumes):
        self.containers = containers
        self.volumes = volumes


class _Meta:
    def __init__(self, annotations):
        self.annotations = annotations


class FakePod:
    """Stands in for the V1Pod that KubeSpawner hands to modify_pod_hook."""

    def __init__(self, containers=None, volumes=None, annotations=None):
        if containers is None:
            containers = [FakeContainer("notebook")]
        self.spec = _Spec(containers, volumes)
        self.metadata = _Meta(annotations)

    @property
    def notebook(self):
        return next(c for c in self.spec.containers if c.name == "notebook")

    def volume_names(self):
        return [v["name"] if isinstance(v, dict) else v.name for v in self.spec.volumes]

    def mount_paths(self):
        return {m["name"]: m["mountPath"] for m in self.notebook.volume_mounts}


def test_mounts_the_users_own_bucket_at_home_gcs():
    p = make_provisioner()
    names = p.names_for("elideng")
    pod = FakePod()
    apply_to_pod(pod, names)

    vol = pod.spec.volumes[0]
    assert vol["csi"]["driver"] == "gcsfuse.csi.storage.gke.io"
    assert vol["csi"]["volumeAttributes"]["bucketName"] == "hdlab-elideng-jupyter-elideng"
    assert pod.mount_paths()["gcs-workspace"] == "/home/jovyan/gcs"


def test_spawner_gets_the_identity_and_nothing_else():
    """apply_to_spawner must set the KSA and stash, but must NOT touch volumes.

    z2jh makes spawner.volumes a *dict* keyed by volume name; the old code did
    list(spawner.volumes), which silently yields the key strings and corrupts the
    volume list instead of failing. Leaving it alone is the fix.
    """
    p = make_provisioner()
    chart_shape = {"volume-elideng": {"name": "volume-elideng"}}
    spawner = FakeSpawner(volumes=dict(chart_shape))
    apply_to_spawner(spawner, p.names_for("elideng"))

    assert spawner.service_account == "jupyter-user-elideng"
    assert spawner.volumes == chart_shape  # untouched, still a dict
    assert spawner._gcs_workspace["names"]["bucket"] == "hdlab-elideng-jupyter-elideng"


def test_mount_survives_a_profile_that_replaces_spawner_volumes():
    """The live failure this whole split exists for.

    KubeSpawner applies kubespawner_override AFTER pre_spawn_hook, and a profile
    supplying `volumes` as a list replaces the trait outright. Injecting at
    modify_pod_hook time means the profile cannot win.
    """
    p = make_provisioner()
    names = p.names_for("elideng")
    spawner = FakeSpawner(volumes={"volume-elideng": {"name": "volume-elideng"}})
    apply_to_spawner(spawner, names)

    # ... profile override lands here, wiping the trait ...
    spawner.volumes = [{"name": "nvidia"}, {"name": "shm"}]

    # ... and the pod is still built with the GCS mount present.
    pod = FakePod(volumes=[{"name": "nvidia"}, {"name": "shm"}])
    apply_to_pod(pod, spawner._gcs_workspace["names"])
    assert "gcs-workspace" in pod.volume_names()


def test_a_pods_home_mount_is_never_dropped():
    """Whatever the pod already had -- home PVC included -- must survive."""
    p = make_provisioner()
    pod = FakePod(
        containers=[
            FakeContainer("notebook", volume_mounts=[
                {"name": "volume-elideng", "mountPath": "/home/jovyan"}
            ])
        ],
        volumes=[{"name": "volume-elideng"}, {"name": "nvidia"}],
    )
    apply_to_pod(pod, p.names_for("elideng"))

    assert pod.volume_names() == ["volume-elideng", "nvidia", "gcs-workspace"]
    assert pod.mount_paths()["volume-elideng"] == "/home/jovyan"


def test_apply_to_pod_is_idempotent():
    p = make_provisioner()
    names = p.names_for("elideng")
    pod = FakePod()
    apply_to_pod(pod, names, shared_bucket="hdlab-elideng-jupyter-shared")
    apply_to_pod(pod, names, shared_bucket="hdlab-elideng-jupyter-shared")
    assert pod.volume_names() == ["gcs-workspace", "gcs-shared"]


def test_a_pod_without_a_notebook_container_fails_closed():
    """Better to refuse the spawn than to return a pod with no ~/gcs."""
    p = make_provisioner()
    pod = FakePod(containers=[FakeContainer("something-else")])
    with pytest.raises(WorkspaceError, match="notebook"):
        apply_to_pod(pod, p.names_for("elideng"))


def test_gcsfuse_mount_options_make_the_directory_writable_by_jovyan():
    opts = gcsfuse_volume("b")["csi"]["volumeAttributes"]["mountOptions"]
    # Without uid/gid the mount is root-owned and read-only in practice.
    # implicit-dirs makes "folders" created by other tools visible.
    assert "uid=1000" in opts and "implicit-dirs" in opts


def test_mount_gid_matches_the_chart_not_the_uid():
    """z2jh runs the notebook as uid 1000 / fsGid 100 -- jovyan:users, not
    jovyan:jovyan. gid=1000 would group-own the mount to a group the user is not
    in; the uid match hides it, so only a test keeps it honest."""
    assert gcs_workspaces.MOUNT_UID == 1000
    assert gcs_workspaces.MOUNT_GID == 100
    opts = gcsfuse_volume("b")["csi"]["volumeAttributes"]["mountOptions"]
    assert "gid=100" in opts and "gid=1000" not in opts


def test_provisioner_is_built_once_per_process():
    """Each build makes a CoreV1Api, which owns an aiohttp session nobody closes.
    Rebuilding per spawn leaks sockets in a process that runs for months."""
    calls = []

    def fake_build():
        calls.append(1)
        return object()

    original, gcs_workspaces._PROVISIONER = gcs_workspaces.build_provisioner, None
    gcs_workspaces.build_provisioner = fake_build
    try:
        first = gcs_workspaces.get_provisioner()
        second = gcs_workspaces.get_provisioner()
    finally:
        gcs_workspaces.build_provisioner = original
        gcs_workspaces._PROVISIONER = None

    assert first is second
    assert len(calls) == 1


def test_existing_profile_volumes_are_preserved():
    """The TCPXO 8-GPU profile brings nvidia/aperture/shm volumes; appending must
    not drop them or the GPU profile stops working."""
    p = make_provisioner()
    pod = FakePod(
        containers=[
            FakeContainer("notebook", volume_mounts=[
                {"name": "nvidia", "mountPath": "/usr/local/nvidia/lib64"}
            ])
        ],
        volumes=[{"name": "nvidia"}, {"name": "shm"}],
        annotations={"networking.gke.io/default-interface": "eth0"},
    )
    apply_to_pod(pod, p.names_for("elideng"))

    assert pod.volume_names() == ["nvidia", "shm", "gcs-workspace"]
    assert pod.notebook.volume_mounts[0]["name"] == "nvidia"
    assert pod.metadata.annotations["networking.gke.io/default-interface"] == "eth0"


def test_sidecar_annotations_include_an_ephemeral_storage_limit():
    p = make_provisioner()
    pod = FakePod()
    apply_to_pod(pod, p.names_for("elideng"))
    ann = pod.metadata.annotations
    assert ann["gke-gcsfuse/volumes"] == "true"
    # gcsfuse stages writes on local disk; this limit is what lets a multi-GB
    # dataset download finish instead of dying partway.
    assert ann["gke-gcsfuse/ephemeral-storage-limit"] == "100Gi"


def test_annotation_values_contain_no_braces():
    """KubeSpawner runs annotation values through str.format(). A literal brace
    is what produced the production KeyError: '"interfaceName"'."""
    for k, v in gcs_workspaces.FUSE_ANNOTATIONS.items():
        assert "{" not in v and "}" not in v, k


def test_shared_bucket_is_mounted_read_only_when_configured():
    p = make_provisioner()
    pod = FakePod()
    apply_to_pod(pod, p.names_for("elideng"), shared_bucket="hdlab-elideng-jupyter-shared")

    shared_vol = [v for v in pod.spec.volumes if v["name"] == "gcs-shared"][0]
    assert shared_vol["csi"]["readOnly"] is True
    shared_mount = [m for m in pod.notebook.volume_mounts if m["name"] == "gcs-shared"][0]
    assert shared_mount["readOnly"] is True
    assert shared_mount["mountPath"] == "/home/jovyan/shared"


def test_mount_options_are_only_flags_gcsfuse_actually_has():
    """Every mountOption is handed to gcsfuse as `--<option>`, so an invented one
    breaks the mount. `read_only` was the real case: the CSI driver reported

        gcsfuse failed with error: Error: unknown flag: --read_only

    and because the sidecar had already started, the notebook container did not
    fail cleanly -- it retried forever on "transport endpoint is not connected"
    and the pod sat Pending until the spawn timed out. Read-only belongs in
    csi.readOnly, which the test above pins. Observed live on 2026-08-27.
    """
    allowed = {"implicit-dirs", "uid", "gid"}
    for read_only in (False, True):
        opts = gcsfuse_volume("b", read_only=read_only)["csi"]["volumeAttributes"][
            "mountOptions"
        ]
        for opt in opts.split(","):
            assert opt.split("=")[0] in allowed, f"unknown gcsfuse flag {opt!r}"


def test_no_shared_mount_when_not_configured():
    p = make_provisioner()
    pod = FakePod()
    apply_to_pod(pod, p.names_for("elideng"))
    assert pod.volume_names() == ["gcs-workspace"]


def test_two_users_get_different_buckets_end_to_end():
    """The isolation invariant, stated as a test of the whole path."""
    gcs, core = FakeGcs(), FakeCoreV1()
    p = make_provisioner(gcs, core)
    a = asyncio.run(p.ensure("alice"))
    b = asyncio.run(p.ensure("bob"))
    assert a["bucket"] != b["bucket"]

    sa, sb = FakeSpawner(), FakeSpawner()
    apply_to_spawner(sa, a)
    apply_to_spawner(sb, b)
    assert sa.service_account != sb.service_account

    pa, pb = FakePod(), FakePod()
    apply_to_pod(pa, a)
    apply_to_pod(pb, b)
    assert (
        pa.spec.volumes[0]["csi"]["volumeAttributes"]["bucketName"]
        != pb.spec.volumes[0]["csi"]["volumeAttributes"]["bucketName"]
    )
    # And neither policy mentions the other's principal.
    for bucket, mine, theirs in ((a["bucket"], a, b), (b["bucket"], b, a)):
        members = [m for bd in gcs.policies[bucket]["bindings"] for m in bd["members"]]
        assert mine["principal"] in members
        assert theirs["principal"] not in members
